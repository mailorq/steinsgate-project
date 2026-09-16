FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LOG_TO_FILES=False

WORKDIR /app

ARG user=django
ARG uid=1000

RUN groupadd -g $uid $user && \
    useradd -m -u $uid -g $user $user && \
    mkdir -p /app/staticfiles /app/media && \
    chown -R $user:$user /app

COPY --chown=$user:$user requirements.txt .

USER $user
ENV PATH="/home/$user/.local/bin:$PATH"

RUN pip install --user -r requirements.txt

COPY --chown=$user:$user . .

EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
