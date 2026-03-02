FROM node:22-slim

RUN npm install -g obsidian-headless

RUN mkdir -p /vault

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
