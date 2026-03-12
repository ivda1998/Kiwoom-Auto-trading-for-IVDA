# Dockerfile - 키움 REST API 트레이딩 봇
# Python 3.11 slim (64bit Linux, PyQt5/OCX 불필요)

FROM python:3.11-slim

# 시스템 패키지 (한국어 로케일 포함)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# 타임존 한국(KST) 설정
ENV TZ=Asia/Seoul
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 작업 디렉토리
WORKDIR /app

# 패키지 먼저 설치 (캐시 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스 코드 복사
COPY . .

# 로그 / 데이터 / 백테스트 디렉토리 생성
RUN mkdir -p logs data backtest_data

# 기본 실행: 봇 메인
CMD ["python", "main.py"]
