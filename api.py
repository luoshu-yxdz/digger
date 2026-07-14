import argparse
import logging
import os
import shutil
import sys
import json
import copy
from configs.base_config import ALLOW_ORIGINS, VERSION, MAGIC_PDF_IMG_URL

from domain.dto.base_dto import BaseResultModel
from domain.dto.output.magic_pdf_parse_main_output import ImageData, MagicPdfParseMainOutput

from services import pdf_service

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import time
from typing import Literal, Union
import uuid
import base64
import uvicorn
from fastapi import  Body, FastAPI, File, Form, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from mineru.cli.backend_options import BACKEND_VLM_ENGINE, DEFAULT_BACKEND

from loguru import logger

BackendOption = Literal[
    "pipeline",
    "vlm-engine",
    "hybrid-engine",
    "vlm-http-client",
    "hybrid-http-client",
]

def create_app(run_mode: str = None):
    app = FastAPI(
        title="API Server",
        version=VERSION,
        docs_url="/swagger",
        redoc_url=None,
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
    # pdf解析
    @app.post("/magic_pdf/magic_pdf_parse_main",description="pdf解析",tags=["Api"])
    async def magic_pdf_parse_main(
        file:UploadFile=File(...),
        parse_method: str = Form('auto'),
        lang: str = Form('ch'),
        backend: BackendOption = Form(BACKEND_VLM_ENGINE)):

        """
        执行从 pdf 转换到 json、md 的过程，输出 md 和 json 文件到 pdf 文件所在的目录
        :param file: .pdf 文件的路径，可以是相对路径，也可以是绝对路径
        :param parse_method: 解析方法， 共 auto、ocr、txt 三种，默认 auto，如果效果不好，可以尝试 ocr
        :param lang: 语言: 
        """
        
        return await pdf_service.magic_pdf_parse_main(file, parse_method, lang_list=[lang], backend=backend)

    # pdf解析(批处理)
    @app.post("/magic_pdf/magic_pdf_parse_main_batch",description="pdf解析(批处理batch_files下的文件)",tags=["Api"])
    async def magic_pdf_parse_main_batch(
        folder_path:str,
        parse_method: str = Form('ocr'),
        lang: str = Form('ch')
        ):
        
        return await pdf_service.magic_pdf_parse_main_batch(parse_method=parse_method,folder_path=folder_path, lang_list=[lang])

    # pdf解析(批处理)
    @app.post("/magic_pdf/magic_pdf_parse_main_batch_all",description="pdf解析(多个路径)",tags=["magic_pdf"])
    async def magic_pdf_parse_main_batch_all(
        folder_paths:str="",
        parse_method: str = Form('ocr'),
        lang: str = Form('ch')
        ):        
        return await pdf_service.magic_pdf_parse_main_batch_all(parse_method=parse_method,folder_paths=folder_paths, lang_list=[lang])

    @app.post("/magic_pdf/upload",description="文件上传",tags=["magic_pdf"])
    async def upload(
            folder_path:str="file_upload",
            file:UploadFile=File(...),
            lang: str = Form('ch'),
            backend: BackendOption = Form(DEFAULT_BACKEND)
        ):
        
        return await pdf_service.magic_pdf_parse_main2(file=file,local_output_path=folder_path, lang_list=[lang], backend=backend)


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
    app = create_app()

    uvicorn.run(app, host=args.host, port=args.port)
