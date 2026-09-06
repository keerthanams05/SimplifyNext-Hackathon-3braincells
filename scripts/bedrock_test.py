"""
Confirms Bedrock model access works for both models the team is planning
to use, before building real agent logic on top of them.

Usage:
    python scripts/bedrock_test.py
"""

import boto3
from botocore.exceptions import ClientError

from config import BEDROCK_REGION, CLAUDE_MODEL_ID, NOVA_MICRO_MODEL_ID

bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

TEST_PROMPT = "In one short sentence, what is a career skill gap?"


def test_model(model_id: str, label: str):
    print(f"\nTesting {label} ({model_id}) in {BEDROCK_REGION} ...")
    try:
        response = bedrock.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": TEST_PROMPT}]}],
            inferenceConfig={"maxTokens": 100, "temperature": 0.3},
        )
        text = response["output"]["message"]["content"][0]["text"]
        print(f"  OK — response: {text.strip()}")
        return True
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "AccessDeniedException":
            print(
                f"  ACCESS DENIED — go to the Bedrock console > Model access, "
                f"and request/enable access to this model in {BEDROCK_REGION}."
            )
        else:
            print(f"  ERROR ({code}): {e.response['Error']['Message']}")
        return False


def main():
    claude_ok = test_model(CLAUDE_MODEL_ID, "Claude Sonnet 4.5")
    nova_ok = test_model(NOVA_MICRO_MODEL_ID, "Amazon Nova Micro")

    print("\nSummary:")
    print(f"  Claude Sonnet 4.5: {'ready' if claude_ok else 'needs model access enabled'}")
    print(f"  Nova Micro:        {'ready' if nova_ok else 'needs model access enabled'}")


if __name__ == "__main__":
    main()