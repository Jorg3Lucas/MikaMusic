#!/bin/bash
# Script de deploy para VPS Linux
# Execute: bash deploy-vps.sh

set -e

echo "=== MikaMusic Bot - Deploy VPS ==="

# 1. Atualizar sistema
echo "[1/6] Atualizando sistema..."
sudo apt update && sudo apt upgrade -y

# 2. Instalar dependências
echo "[2/6] Instalando dependências..."
sudo apt install -y python3 python3-pip python3-venv git ffmpeg

# 3. Clonar repositório (substitua pela sua URL)
echo "[3/6] Clonando repositório..."
if [ ! -d "MikaMusic" ]; then
    git clone https://github.com/SEU_USER/MikaMusic.git
fi
cd MikaMusic

# 4. Criar ambiente virtual e instalar dependências
echo "[4/6] Configurando ambiente Python..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 5. Configurar .env
echo "[5/6] Configurando variáveis de ambiente..."
if [ ! -f ".env" ]; then
    echo "Criando arquivo .env..."
    echo "DISCORD_TOKEN=COLE_SEU_TOKEN_AQUI" > .env
    echo "CLIENT_ID=1541284089770938439" >> .env
    echo "YOUTUBE_API_KEY=" >> .env
    echo ""
    echo "⚠️  Edite o arquivo .env e coloque seu DISCORD_TOKEN!"
    echo "    nano .env"
    echo ""
    echo "💡 Para busca rápida, adicione YOUTUBE_API_KEY (grátis):"
    echo "    https://console.cloud.google.com/apis/credentials"
fi

# 6. Criar serviço systemd para manter rodando
echo "[6/6] Criando serviço systemd..."
sudo tee /etc/systemd/system/mikamusic.service > /dev/null <<EOF
[Unit]
Description=MikaMusic Discord Bot
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/venv/bin/python main.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable mikamusic

echo ""
echo "=== Deploy concluído! ==="
echo ""
echo "Próximos passos:"
echo "1. Edite o .env com seu token: nano .env"
echo "2. Inicie o bot: sudo systemctl start mikamusic"
echo "3. Veja os logs: sudo systemctl status mikamusic"
echo "4. Logs em tempo real: journalctl -u mikamusic -f"
echo ""
echo "Comandos úteis:"
echo "  sudo systemctl start mikamusic    # Iniciar"
echo "  sudo systemctl stop mikamusic     # Parar"
echo "  sudo systemctl restart mikamusic  # Reiniciar"
echo "  sudo systemctl status mikamusic   # Status"
