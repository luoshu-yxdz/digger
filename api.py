import argparse
import logging
import os
import shutil
import sys
import json
import copy
import secrets
from configs.base_config import ALLOW_ORIGINS, VERSION, MAGIC_PDF_IMG_URL, IP_API_KEY_BINDINGS

from domain.dto.base_dto import BaseResultModel
from domain.dto.output.magic_pdf_parse_main_output import ImageData, MagicPdfParseMainOutput

from services import pdf_service

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import time
from typing import Literal, Union
import uuid
import base64
import uvicorn
from fastapi import APIRouter, Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.security import APIKeyHeader
from fastapi.openapi.docs import get_swagger_ui_html
from mineru.cli.backend_options import BACKEND_VLM_ENGINE, DEFAULT_BACKEND

from loguru import logger

BackendOption = Literal[
    "pipeline",
    "vlm-engine",
    "hybrid-engine",
    "vlm-http-client",
    "hybrid-http-client",
]

api_key_header = APIKeyHeader(
    name="api-key",
    auto_error=False,
    description="访问 IP 绑定的密钥非空时必填；配置为空时可不传或传空值，未绑定访问 IP 无需提供。",
)


async def verify_ip_api_key(
    request: Request,
    api_key: str | None = Depends(api_key_header),
) -> None:
    """按访问 URL 的主机名匹配绑定 IP，不使用请求来源 IP 或转发 IP 头。"""
    access_ip = request.url.hostname
    expected_key = IP_API_KEY_BINDINGS.get(access_ip)
    if expected_key is None:
        return

    # 未传请求头与空请求头均按空字符串匹配，支持配置空密钥。
    if not secrets.compare_digest(
        (api_key or "").encode("utf-8"), expected_key.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="缺少或无效的 api-key")


def create_app(run_mode: str = None):
    app = FastAPI(
        title="API Server",
        version=VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOW_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    mount_app_routes(app, run_mode=run_mode)
    
    app.mount("/temp_files", StaticFiles(directory="temp_files"), name="temp_files")

    return app

def mount_app_routes(app: FastAPI, run_mode: str = None):
    # 鉴权仅用于业务接口，Swagger 页面及其 schema 可直接访问。
    api_router = APIRouter(dependencies=[Depends(verify_ip_api_key)])

    # 自定义 Swagger UI
    @app.get("/swagger", include_in_schema=False)
    async def swagger():
        return get_swagger_ui_html(
            openapi_url="/swagger/schema",
            title="API Swagger"
        )

    # 自定义 OpenAPI Schema
    @app.get("/swagger/schema", include_in_schema=False)
    async def swagger_schema():
        return app.openapi()

    # pdf解析
    @api_router.post("/digger/digger_parse_main",description="pdf解析",tags=["Api"])
    async def digger_parse_main(
        file:UploadFile=File(...),
        parse_method: str = Form('auto'),
        lang: str = Form('ch'),
        content_list_version: str = Form('default'),
        backend: BackendOption = Form(BACKEND_VLM_ENGINE)):

        """
        执行从 pdf 转换到 json、md 的过程，输出 md 和 json 文件到 pdf 文件所在的目录
        :param file: .pdf 文件的路径，可以是相对路径，也可以是绝对路径
        :param parse_method: 解析方法， 共 auto、ocr、txt 三种，默认 auto，如果效果不好，可以尝试 ocr
        :param lang: 语言: 
        """
        
        return await pdf_service.magic_pdf_parse_main(
            file,
            parse_method,
            lang_list=[lang],
            backend=backend,
            content_list_version=content_list_version,
        )

    @api_router.post(
        "/digger/digger_parse_main_with_middle_json",
        description="文档解析，返回 content_list 和 middle_json",
        tags=["Api"],
    )
    async def digger_parse_main_with_middle_json(
        file: UploadFile = File(...),
        parse_method: str = Form('auto'),
        lang: str = Form('ch'),
        content_list_version: str = Form('default'),
        backend: BackendOption = Form(BACKEND_VLM_ENGINE),
    ):
        return await pdf_service.magic_pdf_parse_main(
            file,
            parse_method,
            lang_list=[lang],
            backend=backend,
            content_list_version=content_list_version,
            include_middle_json=True,
        )

    # pdf解析(批处理)
    @api_router.post("/digger/digger_parse_main_batch",description="pdf解析(批处理batch_files下的文件)",tags=["Api"])
    async def digger_parse_main_batch(
        folder_path:str,
        parse_method: str = Form('ocr'),
        lang: str = Form('ch')
        ):
        
        return await pdf_service.magic_pdf_parse_main_batch(parse_method=parse_method,folder_path=folder_path, lang_list=[lang])

    # pdf解析(批处理)
    @api_router.post("/digger/digger_parse_main_batch_all",description="pdf解析(多个路径)",tags=["magic_pdf"])
    async def digger_parse_main_batch_all(
        folder_paths:str="",
        parse_method: str = Form('ocr'),
        lang: str = Form('ch')
        ):        
        return await pdf_service.magic_pdf_parse_main_batch_all(parse_method=parse_method,folder_paths=folder_paths, lang_list=[lang])

    @api_router.post("/digger/upload",description="文件上传",tags=["magic_pdf"])
    async def upload(
            folder_path:str="file_upload",
            file:UploadFile=File(...),
            lang: str = Form('ch'),
            backend: BackendOption = Form(DEFAULT_BACKEND)
        ):
        
        return await pdf_service.magic_pdf_parse_main2(file=file,local_output_path=folder_path, lang_list=[lang], backend=backend)

    app.include_router(api_router)


if __name__=="__main__":
    parser = argparse.ArgumentParser(prog='ApiSideCar',
                                     description='ApiSideCar')
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--ssl_keyfile", type=str)
    parser.add_argument("--ssl_certfile", type=str)

    os.environ["MINERU_MODEL_SOURCE"] = "local"

    # 初始化消息
    args = parser.parse_args()
    args_dict = vars(args)
    from services.daily_logging import configure_daily_logging

    configure_daily_logging()
    app = create_app()

    # 禁用代理头解析；鉴权按访问 URL 的 hostname（Host 请求头）匹配。
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=False)
