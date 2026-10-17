FROM nginx:alpine

RUN apk add --no-cache openssl && \
    rm -rf /usr/share/nginx/html/* && \
    rm /etc/nginx/conf.d/default.conf

# Paths are relative to the PROJECT ROOT (the build context)
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY frontend/luma-common.conf /etc/nginx/luma-common.conf
COPY frontend/40-luma-cert.sh /docker-entrypoint.d/40-luma-cert.sh
RUN sed -i 's/\r$//' /docker-entrypoint.d/40-luma-cert.sh /etc/nginx/luma-common.conf /etc/nginx/conf.d/default.conf && \
    chmod +x /docker-entrypoint.d/40-luma-cert.sh
# Serve the Markdown/maths libraries ourselves instead of from a public CDN (blocked on some networks: blank page)
RUN mkdir -p /usr/share/nginx/html/vendor && cd /tmp && \
    wget -q https://registry.npmjs.org/katex/-/katex-0.16.11.tgz && tar xzf katex-0.16.11.tgz && \
    cp -r package/dist /usr/share/nginx/html/vendor/katex && rm -rf package katex-0.16.11.tgz && \
    wget -q https://registry.npmjs.org/marked/-/marked-12.0.0.tgz && tar xzf marked-12.0.0.tgz && \
    cp package/marked.min.js /usr/share/nginx/html/vendor/marked.min.js && rm -rf package marked-12.0.0.tgz
COPY frontend/index.html frontend/style.css frontend/app.js /usr/share/nginx/html/
COPY frontend/assets /usr/share/nginx/html/assets

EXPOSE 80 443

CMD ["nginx", "-g", "daemon off;"]
