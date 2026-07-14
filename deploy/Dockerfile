# MolMind 正式版交付镜像
FROM continuumio/miniconda3:24.1.2-0

WORKDIR /app

# 不钉死小版本：2023.09.6 无 linux-aarch64 包，Apple Silicon 原生构建会失败。
# 与 pyproject.toml 的 rdkit>=2023.9.1 对齐，由 conda 按当前架构解析可用版本。
RUN conda install -y -c conda-forge "rdkit>=2023.09.1" python=3.11 \
    && conda clean -afy

COPY pyproject.toml README.md ./
COPY apps ./apps
COPY services ./services
COPY packages ./packages
COPY configs ./configs
COPY data ./data
COPY templates ./templates
COPY scripts ./scripts

RUN pip install --no-cache-dir pyyaml pandas fastapi "uvicorn[standard]" python-multipart httpx reportlab pytest \
    && pip install --no-cache-dir -e .

ENV PYTHONPATH=/app
ENV MOLMIND_MODE=auto

# 默认断网可复现：依赖 data/evidence_snapshot
CMD ["python", "-m", "apps.cli.main", "--input", "data/sample.sdf", "--output", "output/nomination_top10.csv", "--mode", "offline"]
