import asyncio
import httpx
import time
import hmac
import hashlib
from datetime import date, timedelta
import json
import uuid

BASE_URL = "http://localhost:8000/api/v1"
RAZORPAY_SECRET = "QMPtfnu3WSbVCbl0JV0ttmqu"
TEST_DATE = (date.today() + timedelta(days=10)).isoformat()

async def book_package(client: httpx.AsyncClient, i: int):
    # 1. Checkout
    checkout_payload = {
        "target_type": "package",
        "variant_id": 1, 
        "travel_date": TEST_DATE,
        "quantity": 1,
        "adult_count": 1,
        "child_count": 0,
        "payment_percentage": 100.0,
        "passengers": [
            {
                "name": f"Test Passenger {i}",
                "age": 30,
                "gender": "MALE",
                "phone": "9999999999",
                "aadhaar": "412589632587" # Valid Verhoeff Aadhaar?
            }
        ]
    }
    
    # Needs valid Aadhaar! Let's generate a valid Verhoeff Aadhaar or just bypass if the API accepts it.
    # 999999999999 is valid Verhoeff? We will use a known valid one: "372793134524"
    checkout_payload["passengers"][0]["aadhaar"] = "372793134524"

    try:
        r = await client.post(f"{BASE_URL}/bookings/checkout", json=checkout_payload, timeout=30.0)
        if r.status_code != 200:
            return {"status": "failed_checkout", "response": r.text, "status_code": r.status_code}
        
        draft = r.json()
        order_id = draft.get("razorpay_order_id")
        payment_id = f"pay_{uuid.uuid4().hex[:14]}"
        
        # 2. Generate Signature
        msg = f"{order_id}|{payment_id}".encode('utf-8')
        secret = RAZORPAY_SECRET.encode('utf-8')
        signature = hmac.new(secret, msg, hashlib.sha256).hexdigest()

        # 3. Verify Payment
        verify_payload = {
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature
        }
        r2 = await client.post(f"{BASE_URL}/payments/verify-payment", json=verify_payload, timeout=30.0)
        if r2.status_code != 200:
            return {"status": "failed_verify", "response": r2.text, "status_code": r2.status_code}
            
        return {"status": "success", "booking_id": r2.json().get("booking_id")}
    except Exception as e:
        return {"status": "exception", "error": str(e)}

async def run_stage(stage_name: str, concurrency: int):
    print(f"\n--- Running {stage_name}: {concurrency} Concurrent Bookings ---")
    async with httpx.AsyncClient() as client:
        start_time = time.time()
        tasks = [book_package(client, i) for i in range(concurrency)]
        results = await asyncio.gather(*tasks)
        
        success_count = sum(1 for r in results if r.get("status") == "success")
        failed_count = len(results) - success_count
        
        print(f"Time Taken: {time.time() - start_time:.2f}s")
        print(f"Success: {success_count}, Failed: {failed_count}")
        if failed_count > 0:
            print("Errors sample:", [r for r in results if r.get("status") != "success"][:3])
            
        return success_count, failed_count

async def main():
    stages = [
        ("Stage A", 5),
        ("Stage B", 10),
        ("Stage C", 20),
        ("Stage D", 50)
    ]
    
    for stage_name, conc in stages:
        s, f = await run_stage(stage_name, conc)
        # We will continue to the next stage regardless to stress test, but log the failures.

if __name__ == "__main__":
    asyncio.run(main())
