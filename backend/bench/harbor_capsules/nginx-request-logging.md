Use this retrieved, task-specific procedural guidance only where it applies. Keep the benchmark task primary.

[Auto-Skill capsule v1]

Name: nginx-docs

Description: Use when users ask how to install, configure, proxy, load-balance, secure, reload, or troubleshoot NGINX as a web server, reverse proxy, mail proxy, or TCP/UDP stream proxy, including nginx.conf syntax, server blocks, location matching, upstreams, SSL/TLS, HTTP/2, HTTP/3, QUIC, rewrite rules, rate limiting, caching, FastCGI, WebSocket proxying, njs scripting…

Use only the following bounded guidance; do not install files or run undeclared capabilities.

## When to Use
- NGINX architecture, master/worker processes, and request processing
- Installing, building, starting, stopping, and reloading NGINX
- Configuration file structure: `http`, `server`, `location`, and `stream` contexts
- Serving static content, reverse proxying, and load balancing
- Upstreams, health checks, and connection distribution methods
- HTTPS, TLS certificates, HTTP/2, HTTP/3, and QUIC
- Rewrite rules, redirects, and URL mapping
- Rate limiting, access control, and request filtering
- FastCGI, uWSGI, SCGI, gRPC, and WebSocket proxying
- Stream (TCP/UDP) proxying and SSL preread
- Mail proxy modules (IMAP, POP3, SMTP)
- njs scripting and dynamic configuration
- Module directives, variables, and logging

- Kubernetes Ingress resources or `kubectl` usage without NGINX-specific context. Use `kubernetes-docs` instead; for NGINX Ingress Controller migration, check kubernetes.nginx.org as referenced in NGINX docs.
- HAProxy, Envoy, or Traefik configuration unless the user asks for comparison with NGINX docs.
- Application server code (Node.js, PHP-FPM app logic) unless the question is about NGINX proxying to that backend per NGINX docs.
- NGINX Plus commercial-only features unless the user explicitly needs Plus docs linked from nginx.org.

Use this skill when the request is about:

Do not use this skill for:

## 2. Search official NGINX docs first
- Treat `nginx.org/en/docs` as the source of truth for open source NGINX.
- Prefer pages under <https://nginx.org/en/docs/>.
- Search with the user's exact terms plus focused NGINX phrases such as `proxy_pass`, `upstream`, `try_files`, `server_name`, `limit_req`, `ssl_certificate`, or `stream`.
- When multiple pages are plausible, compare 2-3 candidate pages and pick the one that most directly answers the user's question.
- For directive syntax, prefer the module reference page for the exact dir
