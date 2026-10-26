FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
# no prebuilt aiohttp wheel for arm/v7: install its pure-Python version there
# instead of needing a compiler (ignored where wheels exist)
ENV AIOHTTP_NO_EXTENSIONS=1
RUN pip install --no-cache-dir -r requirements.txt
COPY mergbw.py server.py ./

CMD ["python", "-u", "server.py"]
