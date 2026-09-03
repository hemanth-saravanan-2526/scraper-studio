const path = require("path");
const venv = (exe) => path.join(__dirname, ".venv", "Scripts", exe);

module.exports = {
  apps: [
    {
      name: "scraper-studio-api",
      script: venv("uvicorn.exe"),
      args: "api_service:app --host 0.0.0.0 --port 3683",
      cwd: __dirname,
      interpreter: "none",
      watch: false,
      autorestart: true,
      max_memory_restart: "1G",
      env: { PYTHONUNBUFFERED: "1" },
      error_file: "./logs/api-error.log",
      out_file: "./logs/api-out.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",
    },
    {
      name: "scraper-studio-ui",
      script: venv("streamlit.exe"),
      args: "run streamlit_app.py --server.port 3682 --server.address 0.0.0.0 --server.headless true",
      cwd: __dirname,
      interpreter: "none",
      watch: false,
      autorestart: true,
      max_memory_restart: "1G",
      env: { PYTHONUNBUFFERED: "1" },
      error_file: "./logs/ui-error.log",
      out_file: "./logs/ui-out.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",
    },
    {
      name: "scraper-studio-mcp",
      script: "npx.cmd",
      args: "--yes @playwright/mcp@latest --port 8931 --host 127.0.0.1 --isolated",
      cwd: __dirname,
      interpreter: "none",
      shell: true,
      watch: false,
      autorestart: true,
      max_memory_restart: "512M",
      error_file: "./logs/mcp-error.log",
      out_file: "./logs/mcp-out.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",
    },
  ],
};
