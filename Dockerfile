# Запуск бота в Docker на любом хостинге:
#   docker build -t minibar-bot .
#   docker run --env-file .env -p 8080:8080 minibar-bot
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080
CMD ["python", "bot.py"]
