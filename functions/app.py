from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/")
def index():
    return jsonify({"message": "Hello, World!"})


def handler(event, context):
    from flask import request

    with app.test_request_context(
        path=event["path"],
        base_url=event["headers"]["x-forwarded-proto"]
        + "://"
        + event["headers"]["host"],
        query_string=event["queryStringParameters"],
        method=event["httpMethod"],
        headers=event["headers"],
        data=event["body"],
    ):
        response = app.full_dispatch_request()
        return {
            "statusCode": response.status_code,
            "headers": dict(response.headers),
            "body": response.get_data(as_text=True),
        }
