from datetime import datetime
import os
import sys

'''
服务相关
'''
ALLOW_ORIGINS=["*"]

VERSION="0.0.1"

# 按访问 URL 的 IP（hostname，不含端口）校验 api-key；未绑定的访问 IP 放行。
# 密钥可设为 ""（允许不传或传空值）；nginx 转发需保留 Host：proxy_set_header Host $http_host;
IP_API_KEY_BINDINGS = {
    "120.202.163.221": "BoOWX75TZiB8MxrgtnMaLRnsfTDOgzEPdwIbZQPHHh3cr8aqaaD1V6uc316hDeFr",
}

'''
默认解析临时文件地址
'''
MAGIC_PDF_IMG_URL="http://192.168.88.244:8090"