FROM nginx:alpine

RUN rm -rf /usr/share/nginx/html/* && \
    rm /etc/nginx/conf.d/default.conf

# Paths are relative to the PROJECT ROOT (the build context)
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY frontend/index.html frontend/style.css frontend/app.js /usr/share/nginx/html/
COPY frontend/assets /usr/share/nginx/html/assets

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]