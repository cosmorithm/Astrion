FROM python:3.12.12-slim-bookworm
RUN pip install --no-cache-dir numpy==2.2.6 pandas==2.2.3 matplotlib==3.10.3
COPY container_runner.py /runner/container_runner.py
RUN chmod -R a=rX /runner
ENV PYTHONDONTWRITEBYTECODE=1 MPLBACKEND=Agg MPLCONFIGDIR=/work/.mpl HOME=/tmp
USER 65534:65534
WORKDIR /work
ENTRYPOINT ["python", "-B", "/runner/container_runner.py"]
