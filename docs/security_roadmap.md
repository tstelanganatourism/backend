# Aadhaar Data Security Roadmap (Phase-2 Preparation)

Currently, the `aadhar_number` is stored as plain text for Phase-1 development speed. However, for production and legal compliance (UIDAI guidelines), we must implement the following hardening steps in Phase-2:

## 1. Data Masking (Frontend/Display)
- **Rule**: Never display the full Aadhaar number in the UI.
- **Implementation**: Create a utility function to mask all but the last 4 digits (e.g., `XXXX XXXX 1234`).
- **Scope**: Admin dashboards, Booking summaries, and PDF vouchers.

## 2. Encryption at Rest (Database level)
- **Mechanism**: Use `cryptography.fernet` or PostgreSQL's `pgcrypto` to encrypt the Aadhaar number before saving.
- **Key Management**: Use an environment variable `ENCRYPTION_KEY` (stored in AWS Secrets Manager or similar) to manage the secret.
- **Logic**:
    - `POST /booking`: Encrypt Aadhaar -> Store in DB.
    - `GET /booking`: Fetch from DB -> Decrypt -> Send to Admin.

## 3. Storage Policy (Images)
- **Mechanism**: Cloudflare R2 with signed URLs.
- **Rule**: Aadhaar images must NOT be public. They should be stored in a private R2 bucket.
- **Retrieval**: Generate a short-lived (e.g., 5-minute) pre-signed URL only when an Admin explicitly requests to view the document.

## 4. Tokenization (Optional)
- If we do not need the full number for anything other than verification, store only the **salted hash** of the number for uniqueness checks and discard the original.

> [!IMPORTANT]
> These tasks are scheduled for the "Security Hardening" sprint at the end of Phase-2.
