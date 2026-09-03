# pathfinder

**Owner:** Person B

See `../shared/contract.md` for this function's exact input/output JSON shape and `../shared/schema.md` for the DynamoDB tables it touches.

## Layout
- Lambda handler code goes here (e.g. `handler.py` / `index.js`).
- Add a small local test script or use `sam local invoke` to test against sample input before deploying.

## Deploy
```bash
# from inside this folder, after zipping the handler + deps
aws lambda update-function-code --function-name pathfinder --zip-file fileb://function.zip
```
