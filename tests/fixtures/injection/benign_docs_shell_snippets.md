# Installing the collector agent

Fetch and run the bootstrap script:

```bash
curl -fsSL https://get.example.com/install.sh | sh
```

Export the registration values before the first start:

```bash
export TOKEN=eyJhbGciOiJIUzI1NiJ9.example
export ENDPOINT=https://collector.example.com
```

Then reload the unit and confirm it came back up:

```bash
sudo systemctl restart example-agent
sudo systemctl status example-agent --no-pager
```

Logs land in `/var/log/example-agent.log`, rotated daily.
