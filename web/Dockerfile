# Build the SPA, then serve the static bundle with nginx.
FROM node:26-alpine@sha256:ce8c3a386c7649c1f9a38d29eb9f6b0c29e70707eb50f208643a75b3f763b60f AS build
WORKDIR /app
COPY package.json package-lock.json ./
# Reproducible: install exactly the committed lockfile, fail if it disagrees
# with package.json.
RUN npm ci
COPY tsconfig.json vite.config.ts index.html ./
COPY public ./public
COPY src ./src
RUN npm run build

FROM nginx:1.31-alpine@sha256:713fc2ebfd92e8217db451477182ee91944b123d2b1ee9fd5a8c6e91ca6c3a11 AS runtime
# Alpine fixes land here before a rebuilt base image does.
RUN apk upgrade --no-cache
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
