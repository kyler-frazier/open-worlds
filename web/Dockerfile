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

FROM nginx:1.31.9-alpine@sha256:fb4658584bd9a916ffb6f6e365f97c25cfdb2923801f551ea08356e695f14ff7 AS runtime
# Alpine fixes land here before a rebuilt base image does.
RUN apk upgrade --no-cache
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
