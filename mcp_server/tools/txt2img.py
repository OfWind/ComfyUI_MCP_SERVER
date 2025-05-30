import uuid
import httpx
import asyncio
import json
import os
from mcp_server.utils import load_config, load_prompt_template, randomize_all_seeds
from mcp_server.logger_decorator import log_mcp_call
from mcp_server.logger import default_logger

def _load_default_values():
    """
    从txt2img_api.json中加载默认值
    Load default values from txt2img_api.json
    
    返回:
        dict: 包含默认值的字典
    
    Returns:
        dict: Dictionary containing default values
    """
    try:
        # 获取txt2img_api.json文件路径
        api_json_path = os.path.join(os.path.dirname(__file__), 'txt2img_api.json')
        
        with open(api_json_path, 'r', encoding='utf-8') as f:
            api_json = json.load(f)
        
        # 提取各个字段的默认值，严格对应json结构
        default_prompt = api_json.get("76", {}).get("inputs", {}).get("prompt1", "beautiful scenery nature glass bottle landscape, , purple galaxy bottle,")
        # negative_prompt 健壮性处理
        # raw_negative_prompt = api_json.get("76", {}).get("inputs", {}).get("prompt2", None)
        # if isinstance(raw_negative_prompt, str) and raw_negative_prompt.strip():
        #     default_negative_prompt = raw_negative_prompt.strip()
        # else:
        #     default_negative_prompt = "text, watermark"
        default_width = str(api_json.get("77", {}).get("inputs", {}).get("width", 512))
        default_height = str(api_json.get("77", {}).get("inputs", {}).get("height", 512))
        default_batch_size = str(api_json.get("77", {}).get("inputs", {}).get("batch_size", 1))
        # # model 健壮性处理
        # raw_model = api_json.get("80", {}).get("inputs", {}).get("lora_name", None)
        # if isinstance(raw_model, str) and raw_model.strip():
        #     default_model = raw_model.strip()
        # else:
        #     default_model = "sdxl/albedobaseXL_v21.safetensors"

        return {
            "prompt": default_prompt,
            # "negative_prompt": default_negative_prompt,
            "width": default_width,
            "height": default_height,
            "batch_size": default_batch_size,
            # "model": default_model
        }
    except Exception as e:
        default_logger.error(f"加载默认值时出错: {str(e)}")
        # 如果加载失败，返回原来的默认值
        return {
            "prompt": "beautiful scenery nature glass bottle landscape, , purple galaxy bottle,",
            # "negative_prompt": "text, watermark",
            "width": "512",
            "height": "512",
            "batch_size": "1",
            # "model": "sdxl/albedobaseXL_v21.safetensors"
        }

# 加载默认值
DEFAULT_VALUES = _load_default_values()

def register_txt2img_tool(mcp):
    async def comfyui_txt2img_impl(
            prompt: str, 
            pic_width: str, 
            pic_height: str, 
            # negative_prompt: str, 
            batch_size: str, 
            # model: str
            ) -> str:
        """
        实现ComfyUI文生图API调用，返回Markdown图片格式（异步版）
        支持自定义输出图片宽高、负向提示词、批次、模型。
        """
        default_logger.debug(f"开始处理文生图请求: prompt='{prompt[:50]}...'")
        
        comfyui_host = load_config()
        prompt_template = load_prompt_template('txt2img')
        # seed 处理 | seed processing
        randomize_all_seeds(prompt_template)
        # 正向prompt | positive prompt
        prompt_template["76"]["inputs"]["prompt1"] = prompt


        # 负向prompt | negative prompt
        # 新workflow中不一定有负向prompt节点，需判断
        # 常见负向prompt节点为CLIPTextEncode/NegativePrompt等，这里假设为"7"，如不存在则跳过
        # prompt_template["7"]["inputs"]["text"] = negative_prompt

        # 宽高 | width & height
        if "77" in prompt_template and "inputs" in prompt_template["77"]:
            if "width" in prompt_template["77"]["inputs"]:
                prompt_template["77"]["inputs"]["width"] = int(pic_width)
            if "height" in prompt_template["77"]["inputs"]:
                prompt_template["77"]["inputs"]["height"] = int(pic_height)
            if "batch_size" in prompt_template["77"]["inputs"]:
                prompt_template["77"]["inputs"]["batch_size"] = int(batch_size)

        # 模型 | model
        # prompt_template["4"]["inputs"]["ckpt_name"] = model

        default_logger.debug(f"配置ComfyUI模板参数完成")
        
        client_id = str(uuid.uuid4())
        body = {
            "client_id": client_id,
            "prompt": prompt_template
        }
        
        default_logger.debug(f"开始向ComfyUI发送API请求: {comfyui_host}/api/prompt")
        default_logger.debug(f"请求体内容: {json.dumps(body, ensure_ascii=False, indent=2)}")
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{comfyui_host}/api/prompt", json=body)
            resp.raise_for_status()
            prompt_id = resp.json()["prompt_id"]
            
            default_logger.debug(f"成功提交ComfyUI任务, prompt_id: {prompt_id}")
            
            while True:
                await asyncio.sleep(3)
                history_url = f"{comfyui_host}/api/history/{prompt_id}"
                his_resp = await client.get(history_url)
                his_resp.raise_for_status()
                data = his_resp.json()
                if prompt_id in data:
                    status = data[prompt_id]["status"]
                    if status["completed"] and status["status_str"] == "success":
                        default_logger.debug(f"ComfyUI任务完成: {status['status_str']}")
                        outputs = data[prompt_id]["outputs"]
                        images = None
                        for node_id, node_data in outputs.items():
                            if "images" in node_data:
                                images = node_data["images"]
                                break
                        if images is None:
                            error_msg = "未找到包含images的输出节点 | No output node with images found"
                            default_logger.error(error_msg)
                            raise Exception(error_msg)
                        break
        
        default_logger.debug(f"生成图片数量: {len(images)}")
        
        image_urls = [
            f"{comfyui_host}/api/view?filename={img['filename']}&subfolder={img['subfolder']}&type=output"
            for img in images
        ]
        markdown_images = [f"![image]({url})" for url in image_urls]
        return "\n".join(markdown_images)

    @mcp.tool()
    @log_mcp_call
    async def txt2img(
        prompt: str = DEFAULT_VALUES["prompt"],
        pic_width: str = DEFAULT_VALUES["width"],
        pic_height: str = DEFAULT_VALUES["height"],
        # negative_prompt: str = DEFAULT_VALUES["negative_prompt"],
        batch_size: str = DEFAULT_VALUES["batch_size"],
        # model: str = DEFAULT_VALUES["model"]
    ) -> str:
        """
        Text-to-image service: input prompt, return image in Markdown format (async version).
        Supports custom output image size, negative prompt, batch size, and model (all optional).
        All default values are loaded from txt2img_api.json configuration file.
        Args:
            prompt: str positive prompt
            pic_width: str output image width (optional, default from config)
            pic_height: str output image height (optional, default from config)
            batch_size: str batch size (optional, max 4, default from config)

        Returns:
            str image in Markdown format
        Raises: 
            httpx.RequestError: API request failed
            KeyError: response data format error
            Exception: other unexpected exception
        """
        try:
            default_logger.info(f"接收到文生图请求: prompt='{prompt[:30]}...'")
            result = await comfyui_txt2img_impl(
                prompt, 
                pic_width, 
                pic_height, 
                batch_size)
            default_logger.info(f"文生图请求完成: 生成 {batch_size} 张图片")
            return result
        except httpx.RequestError as e:
            error_msg = f"API请求失败: {str(e)} | API request failed: {str(e)}"
            default_logger.error(error_msg)
            raise Exception(error_msg)
        except KeyError as e:
            error_msg = f"返回数据格式错误: {str(e)} | response data format error: {str(e)}"
            default_logger.error(error_msg)
            raise Exception(error_msg)
        except Exception as e:
            error_msg = f"文生图服务异常: {str(e)} | text-to-image service error: {str(e)}"
            default_logger.error(error_msg)
            raise Exception(error_msg)