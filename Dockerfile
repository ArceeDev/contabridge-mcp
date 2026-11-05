FROM python:3.12-slim
WORKDIR /srv
COPY . .
RUN pip install --no-cache-dir .
ENV CONTABRIDGE_TRANSPORT=http PORT=8080
EXPOSE 8080
USER nobody
CMD ["contabridge-mcp", "--http"]
