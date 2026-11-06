FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build


# Unprivileged-образ: мастер процесс не под root, порт 4173 > 1024, поэтому NET_BIND_SERVICE не нужен
FROM nginxinc/nginx-unprivileged:1.27-alpine

USER root
COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY nginx/security-headers.conf /etc/nginx/security-headers.conf
COPY --from=build /app/dist /usr/share/nginx/html
USER nginx

EXPOSE 4173

CMD ["nginx", "-g", "daemon off;"]
