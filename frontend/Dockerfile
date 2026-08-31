# syntax=docker/dockerfile:1

FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS builder

WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine@sha256:65e3e85dbaed8ba248841d9d58a899b6197106c23cb0ff1a132b7bfe0547e4c0

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /build/dist /usr/share/nginx/html

# nginx-unprivileged already runs as UID 101 and listens on 8080.
EXPOSE 8080
