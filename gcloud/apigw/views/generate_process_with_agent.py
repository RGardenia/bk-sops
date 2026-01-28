# -*- coding: utf-8 -*-
"""
Tencent is pleased to support the open source community by making 蓝鲸智云PaaS平台社区版 (BlueKing PaaS Community
Edition) available.
Copyright (C) 2017 THL A29 Limited, a Tencent company. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at
http://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""
import json
import logging
import os
import re
import requests

from blueapps.account.decorators import login_exempt
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from gcloud import err_code
from gcloud.utils.flow_converter import SimpleFlowConverter

logger = logging.getLogger("root")


def call_agent_api(prompt: str, bk_biz_id: int, username: str = "admin") -> dict:
    """
    调用智能体插件 API 生成流程

    :param prompt: 用户输入的流程描述
    :param bk_biz_id: 业务ID
    :param username: 用户名
    :return: API 响应结果
    """
    # 从环境变量获取配置
    agent_url = os.environ.get("AGENT_PROCESS_BUILD_URL")
    agent_app_code = os.environ.get("AGENT_PROCESS_BUILD_CODE")
    agent_app_secret = os.environ.get("AGENT_PROCESS_BUILD_TOKEN")

    if not agent_url or not agent_app_code:
        raise ValueError("智能体 API 配置缺失，请检查环境变量 AGENT_PROCESS_BUILD_URL 和 AGENT_PROCESS_BUILD_CODE")

    # 构建请求头
    headers = {
        "Content-Type": "application/json",
        "X-Bkapi-Authorization": json.dumps({
            "bk_app_code": agent_app_code,
            "bk_app_secret": agent_app_secret or ""
        }),
        "X-BKAIDEV-USER": username
    }

    # 构建请求体
    input_content = (
        f"角色设定： 你是一名精通标准运维流程编排的专家。你的任务是根据用户描述，结合工具查询结果，生成符合严格规范的流程 JSON 代码。\n\n"
        f"工作流与核心指令：\n\n"
        f"调用工具获取信息（最高优先级）：\n\n"
        f"首先，必须调用 get_plugin_list 工具获取当前可用的插件列表及详细信息。\n\n"
        f"关键约束：流程中所有插件的 code 和 name 字段，必须严格等于 get_plugin_list 工具返回的结果。"
        f"严禁捏造、猜测或使用默认 code 和 name。如果工具返回的结果与预期不符，以工具返回为准。\n\n"
        f"构建流程结构：\n\n"
        f"依据《标准运维流程知识库》进行编排。\n\n"
        f"解析用户输入的流程描述。\n\n"
        f"全局参数 bk_biz_id 统一设置为 {bk_biz_id}。\n\n"
        f"参考'6. 示例' 中的结构进行构建。\n\n"
        f"组件配置规范：\n\n"
        f"Link 组件： 流程中必须包含 Link 组件。\n\n"
        f"Variable 组件： 必须严格按照知识库'3.2.9节'的内容格式进行输出，没有可不输出。\n\n"
        f"输出格式要求：\n\n"
        f"仅输出 JSON 代码，不要包含任何解释性文字、思考过程或前缀后缀。\n\n"
        f"输出必须以 ```json 代码块包裹。\n\n"
        f"JSON 格式必须为列表形式：[{{}}, {{}}...]。\n\n"
        f"JSON 内容不需要转义。\n\n"
        f"生成的 JSON 中必须包含流程命名（name 字段）。\n\n"
        f"输入信息：\n\n"
        f"业务ID：{bk_biz_id}\n\n"
        f"流程描述：{prompt}\n\n"
        f"开始执行： 请先调用工具，获取版本信息，然后生成最终 JSON。"
    )

    request_body = {
        "input": input_content,
        "execute_kwargs": {
            "stream": False
        }
    }

    # 发送请求
    response = requests.post(
        agent_url,
        headers=headers,
        json=request_body,
        timeout=300  # 5分钟超时，因为 AI 生成可能需要较长时间
    )

    logger.info("call_agent_api - Response status: {}".format(response.status_code))
    logger.info("call_agent_api - Response body: {}".format(response.text[:1000] if response.text else ""))

    response.raise_for_status()
    return response.json()


def parse_agent_response(agent_response: dict) -> list:
    """
    解析智能体 API 响应，提取生成的流程 JSON

    :param agent_response: 智能体 API 响应
    :return: 简化流程列表
    """
    if not agent_response.get("result"):
        raise ValueError("智能体 API 返回失败: {}".format(agent_response.get("message", "未知错误")))

    data = agent_response.get("data", {})
    choices = data.get("choices", [])

    if not choices:
        raise ValueError("智能体 API 返回数据为空")

    # 获取 AI 生成的内容
    content = choices[0].get("delta", {}).get("content", "")

    if not content:
        raise ValueError("智能体 API 返回内容为空")

    logger.info("parse_agent_response - Raw content: {}".format(content[:500] if len(content) > 500 else content))

    # 提取 JSON 内容
    # 方法1: 尝试从 markdown 代码块中提取
    json_match = re.search(r'```json\s*([\s\S]*?)\s*```', content)
    if json_match:
        json_content = json_match.group(1).strip()
        logger.info("parse_agent_response - Extracted from ```json block")
    else:
        # 方法2: 尝试从普通代码块中提取
        json_match = re.search(r'```\s*([\s\S]*?)\s*```', content)
        if json_match:
            json_content = json_match.group(1).strip()
            logger.info("parse_agent_response - Extracted from ``` block")
        else:
            # 方法3: 尝试直接查找 JSON 数组 (从第一个 [ 到最后一个 ])
            start_idx = content.find('[')
            end_idx = content.rfind(']')
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                json_content = content[start_idx:end_idx + 1]
                logger.info("parse_agent_response - Extracted JSON array directly")
            else:
                # 方法4: 直接使用原内容
                json_content = content.strip()
                logger.info("parse_agent_response - Using raw content")

    logger.info("parse_agent_response - JSON content to parse: {}".format(
        json_content[:300] if len(json_content) > 300 else json_content))

    simple_flow = json.loads(json_content)

    if not isinstance(simple_flow, list):
        raise ValueError("智能体生成的内容不是有效的 JSON 数组")

    return simple_flow


@require_POST
@login_exempt
@csrf_exempt
def generate_process_with_agent(request):
    """
    将前端简化流程格式转换为标准运维 pipeline_tree 格式

    请求方法: POST
    请求体格式: JSON Object
    {
        "bk_biz_id": 业务ID,
        "simple_flow": [简化流程数组],  // 可选，如果不提供则使用 prompt 调用智能体生成
        "prompt": "流程描述"  // 可选，用于调用智能体生成流程
    }
    """
    try:
        request_data = json.loads(request.body)
    except json.JSONDecodeError as e:
        logger.warning("convert_simple_flow: Invalid JSON format - {}".format(str(e)))
        return JsonResponse({
            "result": False,
            "message": "Invalid JSON format: {}".format(str(e)),
            "code": err_code.REQUEST_PARAM_INVALID.code,
        })

    # 获取 bk_biz_id
    bk_biz_id = request_data.get("bk_biz_id")
    bk_biz_id = 20

    # prompt
    prompt = request_data.get("prompt")
    simple_flow = ""

    # 根据提供的 prompt，则调用智能体 API 生成流程
    if prompt:
        try:
            agent_response = call_agent_api(prompt, bk_biz_id)
            simple_flow = parse_agent_response(agent_response)
        except requests.RequestException as e:
            logger.exception("convert_simple_flow: Agent API request failed - {}".format(str(e)))
            return JsonResponse({
                "result": False,
                "message": "智能体 API 请求失败: {}".format(str(e)),
                "code": err_code.UNKNOWN_ERROR.code,
            })
        except (ValueError, json.JSONDecodeError) as e:
            logger.exception("convert_simple_flow: Agent response parse failed - {}".format(str(e)))
            return JsonResponse({
                "result": False,
                "message": "智能体响应解析失败: {}".format(str(e)),
                "code": err_code.UNKNOWN_ERROR.code,
            })

    if not isinstance(simple_flow, list):
        return JsonResponse({
            "result": False,
            "message": "simple_flow must be a JSON array",
            "code": err_code.REQUEST_PARAM_INVALID.code,
        })

    try:
        converter = SimpleFlowConverter(simple_flow)
        pipeline_tree = converter.convert()
    except KeyError as e:
        logger.exception("convert_simple_flow: Missing required field - {}".format(str(e)))
        return JsonResponse({
            "result": False,
            "message": "Missing required field: {}".format(str(e)),
            "code": err_code.REQUEST_PARAM_INVALID.code,
        })
    except Exception as e:
        logger.exception("convert_simple_flow: Conversion failed - {}".format(str(e)))
        return JsonResponse({
            "result": False,
            "message": "Conversion failed: {}".format(str(e)),
            "code": err_code.UNKNOWN_ERROR.code,
        })

    return JsonResponse({
        "result": True,
        "data": pipeline_tree,
        "code": err_code.SUCCESS.code,
    })
