import uuid
import httpx
import asyncio
import json
import os
import time
from dotenv import load_dotenv  # 新增：支持 .env key 加载
from mcp_server.utils import load_config, load_prompt_template, randomize_all_seeds
from mcp_server.logger_decorator import log_mcp_call
from mcp_server.logger import default_logger

# 默认参数
DEFAULT_ASPECT_RATIO = "16:9"
DEFAULT_GUIDANCE = 3.5
DEFAULT_STEPS = 50

ASPECT_RATIO_MAP = {
    "16:9": "16:9",
    "9:16": "9:16",
    "3:4": "3:4",
    "4:3": "4:3",
}

def _load_default_values():
    try:
        api_json_path = os.path.join(os.path.dirname(__file__), 'imgedit_api.json')
        with open(api_json_path, 'r', encoding='utf-8') as f:
            api_json = json.load(f)
        default_prompt = api_json.get("83", {}).get("inputs", {}).get("prompt", "")
        default_aspect_ratio = api_json.get("83", {}).get("inputs", {}).get("aspect_ratio", DEFAULT_ASPECT_RATIO)
        default_guidance = api_json.get("83", {}).get("inputs", {}).get("guidance", DEFAULT_GUIDANCE)
        default_steps = api_json.get("83", {}).get("inputs", {}).get("steps", DEFAULT_STEPS)
        return {
            "prompt": default_prompt,
            "aspect_ratio": default_aspect_ratio,
            "guidance": default_guidance,
            "steps": default_steps,
        }
    except Exception as e:
        default_logger.error(f"加载默认值时出错: {str(e)}")
        return {
            "prompt": "",
            "aspect_ratio": DEFAULT_ASPECT_RATIO,
            "guidance": DEFAULT_GUIDANCE,
            "steps": DEFAULT_STEPS,
        }

DEFAULT_VALUES = _load_default_values()

# 仅从环境变量读取（支持 MCP Server 自动注入和本地 .env 双重场景）
COMFY_ORG_KEY = os.getenv("COMFY_ORG", "")

async def _upload_image(client, comfyui_host, image_path):
    # 支持本地路径或URL
    if image_path.startswith("http"):
        resp = await client.get(image_path)
        resp.raise_for_status()
        content = resp.content
        filename = os.path.basename(image_path)
    else:
        with open(image_path, "rb") as f:
            content = f.read()
        filename = os.path.basename(image_path)
    files = {"image": (filename, content)}
    upload_url = f"{comfyui_host}/upload/image"
    resp = await client.post(upload_url, files=files)
    resp.raise_for_status()
    # 返回服务器保存的文件名
    return filename

def _get_aspect_ratio_str(aspect_ratio):
    return ASPECT_RATIO_MAP.get(aspect_ratio, DEFAULT_ASPECT_RATIO)

def _get_output_dir_and_filename(save_dir, filename, default_prefix):
    if save_dir:
        if os.path.isdir(save_dir):
            base_output_dir = save_dir
            base_filename_prefix = filename if filename else default_prefix
        else:
            base_output_dir = os.path.dirname(save_dir)
            base_filename_prefix = os.path.splitext(os.path.basename(save_dir))[0]
            if not base_output_dir:
                base_output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'output')
    else:
        base_output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'output')
        base_filename_prefix = filename if filename else default_prefix
    os.makedirs(base_output_dir, exist_ok=True)
    return base_output_dir, base_filename_prefix

def _replace_prompt_template(template, prompt, aspect_ratio, guidance, steps, image1_name, image2_name=None):
    """
    动态组装 prompt_template，可支持一图或两图调用。
    - 单图：template 只含 84 节点和 91 节点 image1，删除 image2 和 102 节点相关内容
    - 双图：自动补全 102 节点，补齐 91 节点 image2 字段，并填充 image2 文件名
    """
    template = json.loads(json.dumps(template))  # 深拷贝

    # 核心节点赋值
    template["83"]["inputs"]["prompt"] = prompt
    template["83"]["inputs"]["aspect_ratio"] = aspect_ratio
    template["83"]["inputs"]["guidance"] = guidance
    template["83"]["inputs"]["steps"] = steps

    # 始终替换 image1
    template["84"]["inputs"]["image"] = image1_name

    if image2_name:
        # 确保有 102 节点（如果 templates 本身没有，要自动补新增）
        if "102" not in template:
            template["102"] = {
                "inputs": {"image": image2_name},
                "class_type": "LoadImage",
                "_meta": {"title": "加载图像"}
            }
        else:
            template["102"]["inputs"]["image"] = image2_name
        # 补齐 91 节点 image2
        if "image2" not in template["91"]["inputs"]:
            template["91"]["inputs"]["image2"] = ["102", 0]
        # 如果被残留的节点误删，补齐
        template["91"]["inputs"]["image2"] = ["102", 0]
    else:
        # 单图：彻底删除 102 节点及 image2 字段，保证结构合法
        if "102" in template:
            del template["102"]
        if "image2" in template["91"]["inputs"]:
            del template["91"]["inputs"]["image2"]

    return template

def register_imgedit_tool(mcp):
    async def comfyui_imgedit_impl(
        prompt: str,
        image1: str,
        image2: str | None,
        aspect_ratio: str,
        guidance: float,
        steps: int,
        save_dir: str | None = None,
        filename: str | None = None
    ) -> str:
        """
        ComfyUI 图像编辑API调用，支持一张或两张图片，保存图片到本地并返回Markdown格式路径
        """
        default_logger.debug(f"开始处理图像编辑请求: prompt='{prompt[:50]}...'")
        comfyui_host = load_config()
        prompt_template = load_prompt_template('imgedit')
        randomize_all_seeds(prompt_template)
        aspect_ratio_str = _get_aspect_ratio_str(aspect_ratio)
        async with httpx.AsyncClient() as client:
            # 上传图片到ComfyUI服务器
            image1_name = await _upload_image(client, comfyui_host, image1)
            image2_name = None
            if image2:
                image2_name = await _upload_image(client, comfyui_host, image2)
            # 替换模板参数
            prompt_template = _replace_prompt_template(
                prompt_template, prompt, aspect_ratio_str, guidance, steps, image1_name, image2_name
            )
            client_id = str(uuid.uuid4())
            # 构造 extra_data 字段，如果 key 存在则加上
            extra_data = {}
            if COMFY_ORG_KEY:
                extra_data["api_key_comfy_org"] = COMFY_ORG_KEY

            body = {
                "client_id": client_id,
                "prompt": prompt_template
            }
            if extra_data:
                body["extra_data"] = extra_data
            default_logger.debug(f"开始向ComfyUI发送API请求: {comfyui_host}/api/prompt")
            try:
                resp = await client.post(f"{comfyui_host}/api/prompt", json=body)
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                default_logger.error(f"ComfyUI 400接口报错，响应内容：{getattr(e.response, 'text', str(e))}")
                default_logger.error(f"ComfyUI 400请求体：{json.dumps(body, ensure_ascii=False, indent=2)}")
                default_logger.error(f"ComfyUI 400 prompt_template：{json.dumps(prompt_template, ensure_ascii=False, indent=2)}")
                raise
            prompt_id = resp.json()["prompt_id"]
            while True:
                await asyncio.sleep(3)
                history_url = f"{comfyui_host}/api/history/{prompt_id}"
                his_resp = await client.get(history_url)
                his_resp.raise_for_status()
                data = his_resp.json()
                if prompt_id in data:
                    status = data[prompt_id]["status"]
                    if status["completed"] and status["status_str"] == "success":
                        outputs = data[prompt_id]["outputs"]
                        images_data = None
                        for node_id, node_data in outputs.items():
                            if "images" in node_data:
                                images_data = node_data["images"]
                                break
                        if images_data is None:
                            raise Exception("未找到包含images的输出节点")
                        # 保存图片
                        base_output_dir, base_filename_prefix = _get_output_dir_and_filename(
                            save_dir, filename, f"imgedit_{int(time.time())}"
                        )
                        local_image_paths = []
                        for i, img_meta in enumerate(images_data):
                            extension = img_meta['filename'].split('.')[-1] if '.' in img_meta['filename'] else 'png'
                            if save_dir and not os.path.isdir(save_dir) and len(images_data) == 1:
                                local_path = save_dir
                            else:
                                if len(images_data) > 1:
                                    final_filename = f"{base_filename_prefix}_{i}.{extension}"
                                else:
                                    final_filename = f"{base_filename_prefix}.{extension}"
                                local_path = os.path.join(base_output_dir, final_filename)
                            img_url = f"{comfyui_host}/api/view?filename={img_meta['filename']}&subfolder={img_meta['subfolder']}&type=output"
                            try:
                                img_resp = await client.get(img_url)
                                img_resp.raise_for_status()
                                with open(local_path, 'wb') as f:
                                    f.write(img_resp.content)
                                local_image_paths.append(local_path)
                                default_logger.debug(f"图片已保存到: {local_path}")
                            except Exception as e:
                                default_logger.error(f"下载图片失败: {str(e)}")
                                local_image_paths.append(img_url)
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
    async def imgedit(
        prompt: str = DEFAULT_VALUES["prompt"],
        image1: str = "",
        image2: str | None = None,
        aspect_ratio: str = DEFAULT_VALUES["aspect_ratio"],
        guidance: float = DEFAULT_VALUES["guidance"],
        steps: int = DEFAULT_VALUES["steps"],
        save_dir: str | None = None,
        filename: str | None = None
    ) -> str:
        """
        图像编辑服务：输入一张或两张图片和描述prompt，生成新图片。支持自定义宽高比、guidance、steps、保存路径和文件名。
        图片路径支持本地绝对路径或URL。
        aspect_ratio 支持: 16:9, 9:16, 3:4, 4:3，默认16:9。
        返回图片本地路径的Markdown格式。
        """
        if not image1:
            raise Exception("必须提供至少一张图片")
        return await comfyui_imgedit_impl(
            prompt, image1, image2, aspect_ratio, guidance, steps, save_dir, filename
        )