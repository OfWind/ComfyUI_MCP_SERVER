import uuid
import httpx
import asyncio
import json
import os
import time  # Added import time here
from mcp_server.utils import load_config, load_prompt_template, randomize_all_seeds
from mcp_server.logger_decorator import log_mcp_call
from mcp_server.logger import default_logger

def _load_default_values():
    """
    从txt2bg_api.json中加载默认值
    Load default values from txt2bg_api.json
    
    返回:
        dict: 包含默认值的字典
    
    Returns:
        dict: Dictionary containing default values
    """
    try:
        # 获取txt2bg_api.json文件路径
        api_json_path = os.path.join(os.path.dirname(__file__), 'txt2bg_api.json')
        
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

def register_txt2bg_tool(mcp):
    async def comfyui_txt2bg_impl(
            prompt: str, 
            pic_width: str, 
            pic_height: str, 
            # negative_prompt: str, 
            batch_size: str, 
            # model: str,
            save_dir: str | None = None, # Added save_dir parameter
            filename: str | None = None # Added filename parameter
            ) -> str:
        """
        实现ComfyUI文生图API调用，保存图片到本地并返回本地路径的Markdown格式（异步版）
        支持自定义输出图片宽高、负向提示词、批次、模型和保存路径。
        """
        default_logger.debug(f"开始处理文生图请求: prompt='{prompt[:50]}...'")
        
        comfyui_host = load_config()
        prompt_template = load_prompt_template('txt2bg')
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
        
        local_image_paths = []  # Initialize here
        images_data = None  # To store images data from API

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
                        images_data = None
                        for node_id, node_data in outputs.items():
                            if "images" in node_data:
                                images_data = node_data["images"]
                                break
                        if images_data is None:
                            error_msg = "未找到包含images的输出节点 | No output node with images found"
                            default_logger.error(error_msg)
                            raise Exception(error_msg)
                        
                        # Image processing and saving moved inside the client context
                        default_logger.debug(f"生成图片数量: {len(images_data)}")
                        
                        # 确定输出目录和基本文件名
                        # Determine output directory and base filename
                        if save_dir:
                            if os.path.isdir(save_dir):
                                base_output_dir = save_dir
                                # 如果指定了目录，但未指定文件名，则生成带时间戳的文件名
                                base_filename_prefix = filename if filename else f"txt2bg_{int(time.time())}"
                            else: # save_dir 是一个文件路径
                                base_output_dir = os.path.dirname(save_dir)
                                # 如果 save_dir 是文件路径，则 filename 参数被忽略，使用 save_dir 的文件名部分
                                base_filename_prefix = os.path.splitext(os.path.basename(save_dir))[0]
                                if not base_output_dir: # 如果 save_dir 是一个文件名，没有目录
                                    base_output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'output')
                        else: # save_dir 未指定
                            base_output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'output')
                            base_filename_prefix = filename if filename else f"txt2bg_{int(time.time())}"
                        
                        os.makedirs(base_output_dir, exist_ok=True)
                        
                        # 下载图片到本地
                        for i, img_meta in enumerate(images_data):
                            image_url = f"{comfyui_host}/api/view?filename={img_meta['filename']}&subfolder={img_meta['subfolder']}&type=output"
                            
                            # 构建本地文件名
                            filename_parts = img_meta['filename'].split('.')
                            extension = filename_parts[-1] if len(filename_parts) > 1 else 'png'
                            
                            current_filename_prefix = base_filename_prefix
                            # 特殊处理：如果 save_dir 是一个完整的文件路径且 batch_size 为 1，则直接使用该路径
                            if save_dir and not os.path.isdir(save_dir) and int(batch_size) == 1:
                                local_path = save_dir 
                            else:
                                # 如果 batch_size > 1，文件名需要添加索引
                                if int(batch_size) > 1:
                                    # 如果原始 base_filename_prefix 已经包含了索引（不太可能，但作为防御性编程）
                                    # 或者用户提供的 filename 已经指定了索引，我们这里统一添加或覆盖索引
                                    # 简单起见，我们总是基于 current_filename_prefix 添加索引
                                    final_filename = f"{current_filename_prefix}_{i}.{extension}"
                                else:
                                    final_filename = f"{current_filename_prefix}.{extension}"
                                local_path = os.path.join(base_output_dir, final_filename)
                            

                            try:
                                img_resp = await client.get(image_url)
                                img_resp.raise_for_status()
                                with open(local_path, 'wb') as f:
                                    f.write(img_resp.content)
                                local_image_paths.append(local_path)
                                default_logger.debug(f"图片已保存到: {local_path}")
                            except Exception as e:
                                default_logger.error(f"下载图片失败: {str(e)}")
                                local_image_paths.append(image_url)  # Fallback to URL
                        break  # Exit while loop once images are processed
        
        # This part remains outside the client context, using populated local_image_paths
        markdown_images = []
        for path in local_image_paths:
            if path.startswith('http'):
                markdown_images.append(f"![image]({path})")
            else:
                abs_path = os.path.abspath(path)
                markdown_images.append(f"![image](file:///{abs_path.replace(os.sep, '/')})")
        
        return "\\n".join(markdown_images)

    @mcp.tool()
    @log_mcp_call
    async def txt2bg(
        prompt: str = DEFAULT_VALUES["prompt"],
        pic_width: str = DEFAULT_VALUES["width"],
        pic_height: str = DEFAULT_VALUES["height"],
        # negative_prompt: str = DEFAULT_VALUES["negative_prompt"],
        batch_size: str = DEFAULT_VALUES["batch_size"],
        # model: str = DEFAULT_VALUES["model"]
        save_dir: str | None = None,
        filename: str | None = None
    ) -> str:
        """
        Background and Scene Generation Service: Generate complete scenes, backgrounds, environments, and landscapes.
        Perfect for creating full backgrounds, natural environments, architectural scenes, fantasy worlds, and complete compositions.
        Images are saved by default to the absolute path of the 'output' directory in the project root
        (e.g., "D:/project/output").
        Supports custom output image size, negative prompt, batch size, model, save directory, and filename (all optional).
        All default values are loaded from txt2bg_api.json configuration file.

        Args:
            prompt (str): Positive prompt describing the background/scene, must be in English.
            pic_width (str): Output image width (optional, default from config).
            pic_height (str): Output image height (optional, default from config).
            batch_size (str): Batch size (optional, max 4, default from config).
            save_dir (str): Optional. Absolute path to the directory where the image(s) should be saved.
                Must be an absolute path (e.g. "D:/my_images" or "/home/user/images"), not a relative path.                
                If None, images are saved in the absolute path of the 'output' directory in the project root
                (e.g., "D:/project/output").
            filename (str | None): Optional. Desired filename for the image (without extension).
                - If batch_size is 1 and filename is provided, this filename is used.
                - If batch_size > 1 and filename is provided, the filename is used as a prefix, followed by an index (e.g., "character_0.png", "character_1.png").
                - If filename is None, a filename is generated based on a timestamp.
                The file extension is determined from the source image or defaults to '.png'.

        Returns:
            str: Local image paths in Markdown format (file:// URLs).

        Raises: 
            httpx.RequestError: API request failed.
            KeyError: Response data format error.
            Exception: Other unexpected exceptions.
        """
        try:
            default_logger.info(f"接收到文生图请求: prompt='{prompt[:30]}...'，保存路径: {save_dir}, 文件名: {filename}")
            result = await comfyui_txt2bg_impl(
                prompt, 
                pic_width, 
                pic_height, 
                batch_size,
                save_dir,
                filename # Pass filename here
            )
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
