from flask import Flask, jsonify
import datetime

app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({
        "status": "success",
        "message": "Привет! Мой первый Docker-контейнер работает!",
        "server_time": str(datetime.datetime.now())
    })

if __name__ == '__main__':
    # Запускаем сервер на порту 5000, открытом для всех адресов (0.0.0.0)
    app.run(host='0.0.0.0', port=5000)
