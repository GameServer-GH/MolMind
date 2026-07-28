# MolMind 运行镜像；默认目标平台 linux/amd64，可用 buildx 同构 arm64。
ARG BASE_IMAGE=continuumio/miniconda3:24.1.2-0
FROM ${BASE_IMAGE}

ARG TARGETPLATFORM
ARG CONDA_CHANNEL=conda-forge
ARG PIP_INDEX_URL=https://pypi.org/simple
LABEL org.molmind.target-platform=${TARGETPLATFORM}

WORKDIR /app

# Embed a real CJK font in mechanism PDFs. Without this, CID-font output can
# render as blank Chinese text in browsers/Poppler even though PDF generation succeeds.
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# 精确固定 RDKit 补丁；镜像源可注入，但默认使用可信官方源。
RUN conda install -y -c "${CONDA_CHANNEL}" "rdkit=2025.09.2" "python=3.11"

COPY pyproject.toml README.md .gitignore .dockerignore ./
COPY deploy ./deploy

# 依赖层仅在锁文件变化时失效，日常代码/测试修改不再重复下载依赖。
RUN pip install --index-url "${PIP_INDEX_URL}" --no-cache-dir -r deploy/requirements.lock

COPY apps ./apps
COPY services ./services
COPY packages ./packages
COPY agent ./agent
COPY plugins ./plugins
COPY configs ./configs
COPY data ./data
COPY templates ./templates
COPY scripts ./scripts
COPY tests ./tests
COPY .github ./.github

RUN pip install --no-deps --no-build-isolation --no-cache-dir -e .

ENV PYTHONPATH=/app
ENV MOLMIND_MODE=auto

# 默认断网可复现：依赖 data/evidence_snapshot
CMD ["python", "-m", "apps.cli.main", "--input", "data/sample.sdf", "--output", "output/nomination_top10.csv", "--mode", "offline"]
