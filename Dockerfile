# Ordis — 轻量自愈运维平台
FROM python:3.11-slim

LABEL org.opencontainers.image.title="ordis"
LABEL org.opencontainers.image.description="轻量自愈运维平台：规则自愈 + AI 诊断 + 自进化技能库"

WORKDIR /opt/ordis

# 系统依赖：procps(ps) iproute2(ss)
RUN apt-get update && apt-get install -y --no-install-recommends \
        procps iproute2 curl && \
    rm -rf /var/lib/apt/lists/*

COPY ordis/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY ordis/ /opt/ordis/ordis/
COPY ordisc /opt/ordis/ordisc
RUN chmod +x /opt/ordis/ordisc && \
    ln -s /opt/ordis/ordisc /usr/local/bin/ordis && \
    ordis --help > /dev/null

# 数据目录（shell历史/模型配置/集群配置），挂卷持久化
ENV ORDIS_HOME=/data
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 9800

# 默认启动集群 server；容器内亦可:
#   docker run ... ordis agent --server http://<server>:9800
#   docker run -it ... ordis talk "..."
CMD ["python3", "/opt/ordis/ordisc", "server", "--port", "9800"]
