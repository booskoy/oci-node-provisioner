import os
import time
import random

import oci
from dotenv import load_dotenv

load_dotenv()

# Setup configuration from environment variables (GitHub Secrets)
config = {
    "user": os.getenv("OCI_USER_ID"),
    "key_content": os.getenv("OCI_PRIVATE_KEY"),
    "fingerprint": os.getenv("OCI_FINGERPRINT"),
    "tenancy": os.getenv("OCI_TENANCY_ID"),
    "region": os.getenv("OCI_REGION"),
}

try:
    compute_client = oci.core.ComputeClient(config)
    identity_client = oci.identity.IdentityClient(config)
    vnic_client = oci.core.VirtualNetworkClient(config)
    print("OCI Authentication Successful. Initializing loop sequence...", flush=True)
except Exception as e:
    print(f"Authentication Failed: {e}", flush=True)
    exit(1)

# Execution parameters
compartment_id = os.getenv("OCI_TENANCY_ID")
subnet_id = os.getenv("OCI_SUBNET_ID")
image_id = os.getenv("OCI_IMAGE_ID")
public_ssh_key = os.getenv("OCI_PUBLIC_SSH_KEY")

# SAFETY CHECK: Verify the key actually loaded from GitHub Secrets
if not public_ssh_key or public_ssh_key.strip() == "":
    print("CRITICAL ERROR: OCI_PUBLIC_SSH_KEY is empty or missing from your secrets!", flush=True)
    exit(1)

# Tunables (можно переопределить в env репозитория/секретах)
ATTEMPTS = int(os.getenv("HUNT_ATTEMPTS", "60"))
SLEEP_CAPACITY = int(os.getenv("HUNT_SLEEP_CAPACITY", "150"))   # после OutOfCapacity
SLEEP_RATE_BASE = int(os.getenv("HUNT_SLEEP_RATE", "300"))      # база бэк-оффа после 429

try:
    ads = [ad.name for ad in
           identity_client.list_availability_domains(compartment_id).data]
    print(f"Found Availability Domains for region {config['region']}: {ads}", flush=True)
except Exception as e:
    print(f"Failed to fetch Availability Domains: {e}", flush=True)
    exit(1)

if not ads:
    print("CRITICAL ERROR: No Availability Domains found in this region.", flush=True)
    exit(1)

# Чередование форм: полный free-кусок (2 OCPU/12 GB) и половинный (1/6).
# Половинный кусок помещается в свободную ёмкость заметно чаще; пойманную
# 1/6 VM можно позже resize до 2/12 из консоли/CLI.
SHAPES = [(2, 12), (1, 6)]

rate_streak = 0


def log(msg):
    print(f"[{time.strftime('%d.%m %H:%M:%S')}] {msg}", flush=True)


def wait_running(inst_id, timeout_s=420):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        st = compute_client.get_instance(inst_id).data.lifecycle_state
        log(f"lifecycle: {st}")
        if st == "RUNNING":
            return True
        if st in ("TERMINATED", "TERMINATING"):
            return False
        time.sleep(20)
    return False


def public_ip_of(inst_id):
    va = compute_client.list_vnic_attachments(
        compartment_id, instance_id=inst_id).data
    if not va:
        return None
    return vnic_client.get_vnic(va[0].vnic_id).data.public_ip


for i in range(1, ATTEMPTS + 1):
    current_ad = ads[(i - 1) % len(ads)]
    ocpus, mem = SHAPES[(i - 1) % len(SHAPES)]
    log(f"[Attempt {i}/{ATTEMPTS}] {current_ad} shape {ocpus} OCPU / {mem} GB")

    try:
        request = oci.core.models.LaunchInstanceDetails(
            display_name="FX-Backend-Server",
            compartment_id=compartment_id,
            availability_domain=current_ad,
            shape="VM.Standard.A1.Flex",
            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=ocpus,
                memory_in_gbs=mem,
            ),
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                source_type="image",
                image_id=image_id,
                boot_volume_size_in_gbs=100,
            ),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=subnet_id,
                assign_public_ip=True,
                assign_private_dns_record=True,
                display_name="forexalertsvnic",
            ),
            metadata={"ssh_authorized_keys": public_ssh_key.strip()},
        )

        response = compute_client.launch_instance(request)
        inst_id = response.data.id
        log(f"LAUNCH ACCEPTED: {inst_id} — жду RUNNING...")
        if wait_running(inst_id):
            ip = public_ip_of(inst_id)
            log("SUCCESS! Instance RUNNING. PUBLIC_IP=" + str(ip))
            log("ssh -i <приватный_ключ> ubuntu@" + str(ip)
                + ("  | shape 1/6: позже resize до 2/12" if ocpus == 1 else ""))
            exit(0)
        log("VM не дошла до RUNNING за 7 мин — терминирую и пробую снова")
        try:
            compute_client.terminate_instance(inst_id)
        except Exception:
            pass
        time.sleep(30)

    except oci.exceptions.ServiceError as e:
        if e.status == 429:
            # Rate limit: экспоненциальный бэк-офф 300/600/900 с,
            # чтобы не жечь попытки впустую (половина лога #65 была 429)
            rate_streak += 1
            pause = min(SLEEP_RATE_BASE * rate_streak, 900)
            log(f"-> Rate Limit (429). Backoff {pause}s (streak {rate_streak})")
            time.sleep(pause)
            continue
        rate_streak = 0
        msg = str(e.message or "")
        if e.status == 500 and "capacity" in msg.lower():
            log(f"-> Capacity Unavailable. Resting {SLEEP_CAPACITY}s + jitter")
            time.sleep(SLEEP_CAPACITY + random.randint(0, 45))
            continue
        if e.status in (400, 401, 403, 404):
            log(f"-> FATAL API Error [{e.status}]: {msg} — не retriable, стоп")
            exit(1)
        log(f"-> API Error [{e.status}]: {msg}. Resting {SLEEP_CAPACITY}s")
        time.sleep(SLEEP_CAPACITY)

    except Exception as e:  # сетевые сбои раннера и пр.
        rate_streak = 0
        log(f"-> Transport error: {e}. Resting {SLEEP_CAPACITY}s")
        time.sleep(SLEEP_CAPACITY)

log(f"loop finished: {ATTEMPTS} attempts, ёмкость в этот прогон не далась")
