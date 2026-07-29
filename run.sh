#!/usr/bin/env bash
# 크루유니언 근태봇 데모 실행 (macOS / Linux)
set -e
cd "$(dirname "$0")"

[ -d venv ] || { echo "[1/3] 가상환경 생성..."; python3 -m venv venv; }
source venv/bin/activate

echo "[2/3] 패키지 설치..."
pip install --upgrade pip >/dev/null
pip install -r requirements.txt

echo "[3/3] 데모 서버 실행..."
python demo.py
