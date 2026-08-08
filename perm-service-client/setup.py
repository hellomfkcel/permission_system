"""perm-service-client — 权限平台通用 Python SDK。

Phase 3: 从 RAG v14 的 PermissionServiceClient 提取为独立 pip 包。
新项目接入只需: pip install + 配置 base_url + api_key + client_id。
"""

from setuptools import setup, find_packages

setup(
    name="perm-service-client",
    version="1.0.0",
    description="通用权限平台客户端 SDK — 多项目支持",
    author="Permission Platform",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=[
        "httpx>=0.27",
        "opentelemetry-api>=1.20",  # optional, for distributed tracing
    ],
    extras_require={
        "tracing": ["opentelemetry-api>=1.20"],
    },
    classifiers=[
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)
