@echo off
echo Starting n8n (Docker)...
docker start n8n 2>nul || docker run -d --name n8n --restart unless-stopped -p 5678:5678 -v n8n_data:/home/node/.n8n -v C:\AI_Factory:/data n8nio/n8n
timeout /t 3 >nul
start http://localhost:5678