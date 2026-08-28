# Deploying with Docker Compose

The stack ships a `compose.yaml` you can copy as-is. Each service declares its own
resource block, and the `system` key holds the host-level limits the container gets.

```yaml
services:
  api:
    image: ghcr.io/example/api:1.4.2
    system:
      memory: 2g
      cpus: 2
    ports:
      - "8080:8080"
  worker:
    image: ghcr.io/example/worker:1.4.2
    system:
      memory: 4g
      cpus: 4
```

Bring the stack up with `docker compose up -d`. The `system` block is read at container
creation time, so a change there needs a recreate rather than a restart.
