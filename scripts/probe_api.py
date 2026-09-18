from remotezip import RemoteZip
import json
URL="https://huggingface.co/datasets/yonathanarbel/SEC_Exhibit_10/resolve/main/By_Year/2001_text.zip"
MEMBER="2001_QTR4_000101287001503256_dex109.txt"
with RemoteZip(URL, headers={"User-Agent":"LicenseBench-POC/0.1"}, timeout=60) as z:
    print("REMOTEZIP_SIZE", z.size())
    names=z.namelist()
    print("REMOTEZIP_ENTRIES", len(names), "MEMBER_FOUND", MEMBER in names)
    raw=z.read(MEMBER)
    print("MEMBER_BYTES", len(raw))
    print("MEMBER_HEAD", raw[:2000].decode("utf-8","replace"))
