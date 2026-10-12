"""Production entry point — waitress WSGI server."""
from waitress import serve

from app import app

if __name__ == "__main__":
    print("CineMatch running on http://127.0.0.1:5210")
    serve(app, host="0.0.0.0", port=5210, threads=6)
