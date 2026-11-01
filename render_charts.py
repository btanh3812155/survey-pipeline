import os
import subprocess

OUTPUT_DIR = "/home/ebylxn/projects/survey-pipeline/docs/charts"
os.makedirs(OUTPUT_DIR, exist_ok=True)

COMMON_HEADER = """
digraph G {
    graph [
        rankdir=LR,
        bgcolor="white",
        dpi=180,
        compound=true,
        nodesep=0.45,
        ranksep=0.75,
        fontname="Helvetica, Arial, sans-serif",
        fontsize=13,
        fontcolor="#111827",
        pad="0.3,0.3"
    ];
    node [
        fontname="Helvetica, Arial, sans-serif",
        fontsize=11,
        fontcolor="#111827",
        shape=box,
        style="rounded,filled",
        fillcolor="#f9fafb",
        color="#9ca3af",
        penwidth=1.5,
        margin="0.2,0.12"
    ];
    edge [
        fontname="Helvetica, Arial, sans-serif",
        fontsize=10,
        fontcolor="#4b5563",
        color="#4b5563",
        penwidth=1.3,
        arrowsize=0.85
    ];
"""

# Chart 1: Abstract Overall Flow
CHART_1 = COMMON_HEADER + """
    subgraph cluster_edge {
        label="1. Edge Tier (Vietnam)";
        style="rounded,filled";
        fillcolor="#f3f4f6";
        color="#d1d5db";
        fontcolor="#1f2937";
        
        driver [label="Driver on Mobile\\n(Lark Form + Private Telegram Bot)", fillcolor="#e0f2fe", color="#0284c7"];
        staff [label="Management on Company PCs\\n(Lark Base Workspace Dashboard)", fillcolor="#ede9fe", color="#7c3aed"];
        clients [label="Paying Business Clients\\n(Google Drive Deliverable Folder)", fillcolor="#fef3c7", color="#d97706"];
    }

    subgraph cluster_cloud {
        label="2. Cloud Processing Core (Singapore SGP1)";
        style="rounded,filled";
        fillcolor="#f8fafc";
        color="#cbd5e1";
        fontcolor="#1e293b";

        caddy [label="Caddy TLS 1.3 Reverse Proxy\\n(25MB Request Cap)", fillcolor="#ffffff", color="#64748b"];
        fastapi [label="FastAPI Ingestion Gateway\\n(4 Uvicorn Workers)", fillcolor="#e0f2fe", color="#0284c7"];
        redis [label="Redis 7 Cache & Queue\\n(Session Locks & 3/8h Appeal Limit)", fillcolor="#fee2e2", color="#dc2626"];
        sanitizer [label="In-Memory Sanitizer\\n(EXIF, ELA, Blur, Exposure)", fillcolor="#ffffff", color="#64748b"];
        dual_ai [label="Dual AI Extraction\\nPrimary: DeepSeek Flash\\nBackup: Gemini 3.5 (No Reasoning)", fillcolor="#e0e7ff", color="#4338ca"];
        rules [label="Python Deterministic Rules\\n(Immune to Prompt Injection)", fillcolor="#dcfce7", color="#16a34a"];
        outbox_worker [label="Background Outbox Worker\\n(SKIP LOCKED Poller)", fillcolor="#fef3c7", color="#d97706"];
    }

    subgraph cluster_storage {
        label="3. Storage & Downstream Outputs";
        style="rounded,filled";
        fillcolor="#f3f4f6";
        color="#d1d5db";
        fontcolor="#1f2937";

        postgres [label="PostgreSQL Database\\n(survey_submissions + outbox_events)", fillcolor="#e0f2fe", color="#0284c7"];
        r2 [label="Cloudflare R2 Storage\\n(Permanent $0 Egress Archive)", fillcolor="#fef3c7", color="#d97706"];
        gdrive [label="Google Drive Folder\\n(Curated Client Spreadsheets & Photos)", fillcolor="#dcfce7", color="#16a34a"];
        lark_cloud [label="Bytedance Lark Cloud\\n(Admin Grid, QC Kanban, Telesales)", fillcolor="#ede9fe", color="#7c3aed"];
        zns [label="Zalo ZNS Service\\n(Thank-You to Driver Phone - No Consent)", fillcolor="#e0f2fe", color="#0284c7"];
        admin_tg [label="Telegram Admin Mod Group\\n(1-Click Moderation Buttons)", fillcolor="#fee2e2", color="#dc2626"];
    }

    driver -> caddy [label="Submit Form"];
    caddy -> fastapi;
    fastapi -> redis [dir=both, label="Lock / Cache"];
    fastapi -> sanitizer -> dual_ai -> rules;
    rules -> postgres [label="Approved (Atomic Commit)"];
    rules -> driver [label="Rejected / Retake Deep Link", style=dashed, color="#dc2626"];
    rules -> admin_tg [label="Appealed (Max 3/8h)", style=dashed, color="#d97706"];
    postgres -> outbox_worker [dir=both];
    outbox_worker -> r2;
    outbox_worker -> gdrive -> clients;
    outbox_worker -> lark_cloud;
    lark_cloud -> staff [dir=both, label="Manage / Review"];
    outbox_worker -> zns -> driver [label="ZNS Thank-You"];
    admin_tg -> fastapi [label="1-Click Override", color="#dc2626"];
}
"""

# Chart 2: Component 1 Ingress (Balanced TB layout)
CHART_2 = """
digraph G {
    graph [rankdir=TB, bgcolor="white", dpi=180, nodesep=0.4, ranksep=0.45, fontname="Helvetica, Arial, sans-serif", fontsize=13, fontcolor="#111827", pad="0.3,0.3"];
    node [fontname="Helvetica, Arial, sans-serif", fontsize=11, fontcolor="#111827", shape=box, style="rounded,filled", fillcolor="#f9fafb", color="#9ca3af", penwidth=1.5, margin="0.2,0.12"];
    edge [fontname="Helvetica, Arial, sans-serif", fontsize=10, fontcolor="#4b5563", color="#4b5563", penwidth=1.3, arrowsize=0.85];

    req [label="Incoming HTTP POST Webhook\\n(From Lark Base or Telegram)", fillcolor="#e0f2fe", color="#0284c7"];
    caddy [label="Caddy Reverse Proxy (DO SGP1)\\n(TLS 1.3 Termination, 25MB Body Cap)", fillcolor="#ffffff", color="#64748b"];
    route [label="Endpoint Route Check", shape=diamond, fillcolor="#fef3c7", color="#d97706"];

    subgraph cluster_lark {
        label="Bytedance Lark Base Webhook Flow";
        style="rounded,filled"; fillcolor="#f8fafc"; color="#cbd5e1";
        challenge [label="URL Challenge Handshake?", shape=diamond, fillcolor="#fef3c7", color="#d97706"];
        echo_chal [label="Echo Challenge Token (< 1s)\\n200 OK", fillcolor="#dcfce7", color="#16a34a"];
        raw_hash [label="Raw SHA256 String Hash\\nsha256(ts + nonce + key + body)", fillcolor="#ffffff", color="#64748b"];
        verify_sig [label="Compare X-Lark-Signature\\n(Constant-Time secrets.compare_digest)", shape=diamond, fillcolor="#fef3c7", color="#d97706"];
        drop_401 [label="Drop Request:\\n401 Unauthorized", fillcolor="#fee2e2", color="#dc2626"];
        redis_lock [label="Redis SETNX Lock\\nlock:submission:ID (60s TTL)", shape=diamond, fillcolor="#fef3c7", color="#d97706"];
        drop_dup [label="Duplicate Webhook Retry:\\nReturn 200 OK & Exit", fillcolor="#fef3c7", color="#d97706"];
        mem_stream [label="Stream Image Bytes to Memory\\n(io.BytesIO - Zero Disk I/O)", fillcolor="#dcfce7", color="#16a34a"];
    }

    subgraph cluster_telegram {
        label="Telegram Bot Webhook Flow";
        style="rounded,filled"; fillcolor="#f8fafc"; color="#cbd5e1";
        tg_auth [label="Verify Telegram Secret Token\\nX-Telegram-Bot-Api-Secret-Token", shape=diamond, fillcolor="#fef3c7", color="#d97706"];
        tg_drop [label="Drop Request: 401 Unauthorized", fillcolor="#fee2e2", color="#dc2626"];
        tg_route [label="Route to Driver 1-to-1 DM Handler\\nor Admin Mod Group Relay", fillcolor="#e0f2fe", color="#0284c7"];
    }

    req -> caddy -> route;
    route -> challenge [label="/webhook/lark-survey"];
    challenge -> echo_chal [label="Yes"];
    challenge -> raw_hash [label="No (Normal Event)"];
    raw_hash -> verify_sig;
    verify_sig -> drop_401 [label="Invalid"];
    verify_sig -> redis_lock [label="Valid"];
    redis_lock -> drop_dup [label="Key Exists"];
    redis_lock -> mem_stream [label="New Key -> Pass to Normalizer"];

    route -> tg_auth [label="/webhook/telegram"];
    tg_auth -> tg_drop [label="Invalid"];
    tg_auth -> tg_route [label="Valid"];
}
"""

# Chart 3: Component 2 Normalization & Quality (Balanced TB layout)
CHART_3 = """
digraph G {
    graph [rankdir=TB, bgcolor="white", dpi=180, nodesep=0.35, ranksep=0.45, fontname="Helvetica, Arial, sans-serif", fontsize=13, fontcolor="#111827", pad="0.3,0.3"];
    node [fontname="Helvetica, Arial, sans-serif", fontsize=11, fontcolor="#111827", shape=box, style="rounded,filled", fillcolor="#f9fafb", color="#9ca3af", penwidth=1.5, margin="0.2,0.12"];
    edge [fontname="Helvetica, Arial, sans-serif", fontsize=10, fontcolor="#4b5563", color="#4b5563", penwidth=1.3, arrowsize=0.85];

    raw_in [label="Raw Image Bytes in Memory (io.BytesIO)", fillcolor="#e0f2fe", color="#0284c7"];

    subgraph cluster_forensic {
        label="Stage 1: Forensic Scan (Executed on Raw Bytes First!)";
        style="rounded,filled"; fillcolor="#f8fafc"; color="#cbd5e1";
        exif_scan [label="Scan EXIF Tag 305\\n(Detect Photoshop, Canva, Snapseed, Lightroom)", fillcolor="#ffffff", color="#64748b"];
        ela_scan [label="Error Level Analysis (ELA)\\n(Detect Localized Splicing/Cloning)", fillcolor="#ffffff", color="#64748b"];
        tamper_gate [label="Digital Tampering Found?", shape=diamond, fillcolor="#fef3c7", color="#d97706"];
        rej_tamper [label="Liberal Reject:\\nDIGITAL_TAMPERING", fillcolor="#fee2e2", color="#dc2626"];
    }

    subgraph cluster_normalize {
        label="Stage 2: In-Memory Normalization & Bug Elimination";
        style="rounded,filled"; fillcolor="#f8fafc"; color="#cbd5e1";
        draft_scale [label="libjpeg C Draft Scaling\\nim.draft('RGB', (1024, 1024))\\n(85% RAM Reduction)", fillcolor="#ffffff", color="#64748b"];
        exif_trans [label="Pillow exif_transpose\\n(Auto-Rotate Mobile Orientation)", fillcolor="#ffffff", color="#64748b"];
        purge_exif [label="Purge EXIF Metadata (exif=b'')\\n(Kills Double-Rotation Bug)", fillcolor="#ffffff", color="#64748b"];
        alpha_canvas [label="White Canvas Alpha Composite\\n(Kills RGBA/P Blackout Bug)", fillcolor="#ffffff", color="#64748b"];
        webp_enc [label="Lanczos 1024px WebP Encoding\\n(Quality 82, ~49KB Payload)", fillcolor="#ffffff", color="#64748b"];
    }

    subgraph cluster_quality {
        label="Stage 3: Physical Quality & Exposure Pre-Filters";
        style="rounded,filled"; fillcolor="#f8fafc"; color="#cbd5e1";
        blur_gate [label="OpenCV Laplacian Blur Check\\n(Variance >= 100.0)", shape=diamond, fillcolor="#fef3c7", color="#d97706"];
        rej_blur [label="Liberal Reject:\\nIMAGE_TOO_BLURRY", fillcolor="#fee2e2", color="#dc2626"];
        is_screen [label="Digital UI Screenshot Check\\n(is_digital_screenshot)", shape=diamond, fillcolor="#fef3c7", color="#d97706"];
        exempt_screen [label="EXEMPT From Exposure Checks\\n(Dark Mode & Light Themes 100% Safe)", fillcolor="#dcfce7", color="#16a34a"];
        p95_check [label="Physical Photo: 95th Percentile\\np95 Peak Brightness < 35?", shape=diamond, fillcolor="#fef3c7", color="#d97706"];
        rej_dark [label="Liberal Reject:\\nIMAGE_TOO_DARK", fillcolor="#fee2e2", color="#dc2626"];
        glare_check [label="Specular Glare Check\\n(Flash Halo Destroys Text Edges?)", shape=diamond, fillcolor="#fef3c7", color="#d97706"];
        rej_glare [label="Liberal Reject:\\nSPECULAR_GLARE", fillcolor="#fee2e2", color="#dc2626"];
        pass_quality [label="Quality Gate PASSED\\n(Hand off to Fleet Screening & AI)", fillcolor="#dcfce7", color="#16a34a"];
    }

    raw_in -> exif_scan -> ela_scan -> tamper_gate;
    tamper_gate -> rej_tamper [label="Yes"];
    tamper_gate -> draft_scale [label="No (Clean)"];
    draft_scale -> exif_trans -> purge_exif -> alpha_canvas -> webp_enc -> blur_gate;
    blur_gate -> rej_blur [label="Variance < 100"];
    blur_gate -> is_screen [label="Sharp (Variance >= 100)"];
    is_screen -> exempt_screen [label="Yes (App Screenshot)"];
    exempt_screen -> pass_quality;
    is_screen -> p95_check [label="No (Camera Shot)"];
    p95_check -> rej_dark [label="p95 < 35 (Pitch Black)"];
    p95_check -> glare_check [label="p95 >= 35"];
    glare_check -> rej_glare [label="Text Obliterated"];
    glare_check -> pass_quality [label="Text Sharp & Legible"];
}
"""

# Chart 4: Component 3 Fleet Anti-Fraud & Dual AI (Balanced TB layout)
CHART_4 = """
digraph G {
    graph [rankdir=TB, bgcolor="white", dpi=180, nodesep=0.35, ranksep=0.45, fontname="Helvetica, Arial, sans-serif", fontsize=13, fontcolor="#111827", pad="0.3,0.3"];
    node [fontname="Helvetica, Arial, sans-serif", fontsize=11, fontcolor="#111827", shape=box, style="rounded,filled", fillcolor="#f9fafb", color="#9ca3af", penwidth=1.5, margin="0.2,0.12"];
    edge [fontname="Helvetica, Arial, sans-serif", fontsize=10, fontcolor="#4b5563", color="#4b5563", penwidth=1.3, arrowsize=0.85];

    img_in [label="Sanitized 1024px WebP Image", fillcolor="#e0f2fe", color="#0284c7"];

    subgraph cluster_fleet_fraud {
        label="Stage 1: Multi-Layer Fleet Fraud Screening";
        style="rounded,filled"; fillcolor="#f8fafc"; color="#cbd5e1";
        dhash [label="dHash Stock Image Match?\\n(Hamming Distance <= 6)", shape=diamond, fillcolor="#fef3c7", color="#d97706"];
        rej_stock [label="Liberal Reject:\\nSTOCK_IMAGE_DETECTED", fillcolor="#fee2e2", color="#dc2626"];
        fft [label="2D FFT Spectral Analysis\\n(Diffusion Ratio > 1.35?)", shape=diamond, fillcolor="#fef3c7", color="#d97706"];
        rej_ai [label="Liberal Reject:\\nAI_GENERATED_DETECTED", fillcolor="#fee2e2", color="#dc2626"];
        veh_tracker [label="Vehicle Persistence Tracker\\n(Match Plate & Type in DB)", shape=diamond, fillcolor="#fef3c7", color="#d97706"];
        rej_veh [label="Liberal Reject:\\nVEHICLE_MISMATCH", fillcolor="#fee2e2", color="#dc2626"];
        income_check [label="Income Sanity Engine\\n(Bounds & Micro-Ride Math)", shape=diamond, fillcolor="#fef3c7", color="#d97706"];
        rej_income [label="Liberal Reject:\\nOUTLANDISH_INCOME", fillcolor="#fee2e2", color="#dc2626"];
    }

    subgraph cluster_dual_ai {
        label="Stage 2: Dual-Model AI Extraction (Primary -> Failover)";
        style="rounded,filled"; fillcolor="#f8fafc"; color="#cbd5e1";
        ai_router [label="AI Failover Router", shape=diamond, fillcolor="#fef3c7", color="#d97706"];
        ds_flash [label="Primary: DeepSeek Flash\\n(Alibaba Cloud SG / API, 2.0s Limit)", fillcolor="#e0e7ff", color="#4338ca"];
        gemini_flash [label="Backup: Gemini 3.5 Flash\\n(STRICTLY NO REASONING: thinking_budget=0)", fillcolor="#e0f2fe", color="#0284c7"];
        json_out [label="Raw Factual JSON Extraction\\n(Zero Math in LLM)", fillcolor="#ffffff", color="#64748b"];
    }

    subgraph cluster_eval_rules {
        label="Stage 3: Deterministic Compliance Evaluation";
        style="rounded,filled"; fillcolor="#f8fafc"; color="#cbd5e1";
        py_rules [label="Python Deterministic Rule Engine\\n(Validates Math, Signature, Tax Code)", shape=diamond, fillcolor="#fef3c7", color="#d97706"];
        rej_rules [label="Liberal Reject:\\nMISSING_FIELDS / ARITHMETIC_MISMATCH", fillcolor="#fee2e2", color="#dc2626"];
        approved [label="Survey Fully Approved!\\n(Hand off to Persistence)", fillcolor="#dcfce7", color="#16a34a"];
    }

    img_in -> dhash;
    dhash -> rej_stock [label="Matched Stock"];
    dhash -> fft [label="Unique"];
    fft -> rej_ai [label="Synthetic AI"];
    fft -> veh_tracker [label="Natural Photo"];
    veh_tracker -> rej_veh [label="Plate Mismatch"];
    veh_tracker -> income_check [label="Vehicle Matches"];
    income_check -> rej_income [label="Outlandish Math"];
    income_check -> ai_router [label="Realistic Income"];

    ai_router -> ds_flash [label="Try Primary"];
    ds_flash -> json_out [label="Success (< 2.0s)"];
    ds_flash -> gemini_flash [label="Timeout / 429 / 503", color="#dc2626"];
    gemini_flash -> json_out [label="Instant Fallback"];

    json_out -> py_rules;
    py_rules -> rej_rules [label="Failed Compliance"];
    py_rules -> approved [label="Passed Compliance"];
}
"""

# Chart 5: Component 4 Persistence & Delivery
CHART_5 = COMMON_HEADER + """
    subgraph cluster_persistence {
        label="Component 4: Persistence, Transactional Outbox & Delivery";
        style="rounded,filled";
        fillcolor="#f8fafc";
        color="#cbd5e1";

        approved_in [label="Approved Survey Event", fillcolor="#dcfce7", color="#16a34a"];
        sql_tx [label="PostgreSQL Atomic Transaction\\n(BEGIN ... COMMIT)", fillcolor="#ffffff", color="#64748b"];
        sub_table [label="INSERT survey_submissions\\n(status = APPROVED)", fillcolor="#e0f2fe", color="#0284c7"];
        outbox_table [label="INSERT outbox_events\\n(status = PENDING)", fillcolor="#fee2e2", color="#dc2626"];

        poller [label="Background Outbox Worker\\n(SELECT FOR UPDATE SKIP LOCKED)", fillcolor="#fef3c7", color="#d97706"];
        r2_upload [label="Cloudflare R2 Object Storage\\n(Upload WebP, $0 Egress)", fillcolor="#ffffff", color="#64748b"];
        ledger_credit [label="Credit Payout to Ledger\\ncollaborator_billing_ledger", fillcolor="#dcfce7", color="#16a34a"];

        gdrive_sync [label="Google Drive API Delivery\\n(Service Account Upload)", fillcolor="#e0e7ff", color="#4338ca"];
        gdrive_folder [label="Customer Delivery Folder\\n(YYYY-MM-DD/Client_Spreadsheet.xlsx)", fillcolor="#dcfce7", color="#16a34a"];

        lark_sync [label="Lark Base Bi-directional Sync\\n(Updates Management Dashboard)", fillcolor="#ede9fe", color="#7c3aed"];
        zns_notify [label="Zalo ZNS Official Thank-You\\n(Delivered to Driver Phone - No Consent)", fillcolor="#e0f2fe", color="#0284c7"];
    }

    approved_in -> sql_tx;
    sql_tx -> sub_table;
    sql_tx -> outbox_table;
    outbox_table -> poller [label="Poll Events"];
    poller -> r2_upload;
    poller -> ledger_credit;
    poller -> gdrive_sync -> gdrive_folder;
    poller -> lark_sync;
    poller -> zns_notify;
}
"""

# Chart 6: Component 5 Telegram UX & Moderation (Balanced TB layout)
CHART_6 = """
digraph G {
    graph [rankdir=TB, bgcolor="white", dpi=180, nodesep=0.35, ranksep=0.45, fontname="Helvetica, Arial, sans-serif", fontsize=13, fontcolor="#111827", pad="0.3,0.3"];
    node [fontname="Helvetica, Arial, sans-serif", fontsize=11, fontcolor="#111827", shape=box, style="rounded,filled", fillcolor="#f9fafb", color="#9ca3af", penwidth=1.5, margin="0.2,0.12"];
    edge [fontname="Helvetica, Arial, sans-serif", fontsize=10, fontcolor="#4b5563", color="#4b5563", penwidth=1.3, arrowsize=0.85];

    subgraph cluster_driver_retake {
        label="Stage 1: Driver Feedback & 1-Click Retake (Private 1-to-1 DM)";
        style="rounded,filled"; fillcolor="#f8fafc"; color="#cbd5e1";
        rej_in [label="Survey Rejection Notice\\n(Exact Retake Guidance)", fillcolor="#fee2e2", color="#dc2626"];
        deep_link [label="1-Click Telegram Deep Link\\nt.me/bot?start=fix_ID_slot", fillcolor="#ffffff", color="#64748b"];
        priv_dm [label="Driver Taps Link -> Opens Private 1-to-1 DM\\n(Driver strictly sees ONLY their own chat!)", fillcolor="#e0f2fe", color="#0284c7"];
        snap_fix [label="Driver Snaps & Sends Replacement Photo\\n(Patches Slot in DB & R2 -> Approved!)", fillcolor="#dcfce7", color="#16a34a"];
    }

    subgraph cluster_appeal_limit {
        label="Stage 2: Rate-Limited Human Appeal (Max 3 / 8 Hours)";
        style="rounded,filled"; fillcolor="#f8fafc"; color="#cbd5e1";
        appeal_btn [label="Driver Taps Inline Button:\\n🙋 Yêu cầu người thật kiểm tra lại", fillcolor="#fef3c7", color="#d97706"];
        redis_limit [label="Redis Counter Check:\\nAppeals in Last 8 Hours?", shape=diamond, fillcolor="#fef3c7", color="#d97706"];
        block_appeal [label="Block Appeal (> 3 in 8h):\\nBẠN ĐÃ DÙNG HẾT LƯỢT KHIẾU NẠI TRONG 8H", fillcolor="#fee2e2", color="#dc2626"];
        allow_appeal [label="Allow Appeal (<= 3 in 8h):\\nIncrement Counter (8h TTL) & status=APPEAL_PENDING", fillcolor="#dcfce7", color="#16a34a"];
    }

    subgraph cluster_mod_group {
        label="Stage 3: Telegram Admin Moderation Group & 3-Mode Dispatch";
        style="rounded,filled"; fillcolor="#f8fafc"; color="#cbd5e1";
        admin_group [label="Telegram Admin Moderation Group\\n(Alerts with Photo + Driver Phone + Reason)", fillcolor="#fee2e2", color="#dc2626"];
        mod_action [label="Admins Moderate with 1-Click Buttons\\n[Duyệt ép] [Từ chối] [Cảnh cáo]", fillcolor="#ffffff", color="#64748b"];
        mod_dispatch [label="Moderator Dispatch Controls", shape=diamond, fillcolor="#fef3c7", color="#d97706"];
        mode1 [label="Mode 1: Internal Mod Note\\n(Stays in Admin Group Only)", fillcolor="#f3f4f6", color="#6b7280"];
        mode2 [label="Mode 2: Targeted Message (/send)\\n(Relayed Privately to Driver 1-to-1 DM)", fillcolor="#e0f2fe", color="#0284c7"];
        mode3 [label="Mode 3: Fleet Broadcast (/broadcast)\\n(Pushed Individually to All Driver DMs)", fillcolor="#ede9fe", color="#7c3aed"];
    }

    rej_in -> deep_link -> priv_dm -> snap_fix;
    rej_in -> appeal_btn -> redis_limit;
    redis_limit -> block_appeal [label="> 3 Appeals"];
    redis_limit -> allow_appeal [label="<= 3 Appeals"];
    allow_appeal -> admin_group -> mod_action;

    admin_group -> mod_dispatch;
    mod_dispatch -> mode1 [label="Internal Chat"];
    mod_dispatch -> mode2 [label="Direct Reply"];
    mod_dispatch -> mode3 [label="Broadcast"];
    mode2 -> priv_dm;
    mode3 -> priv_dm;
}
"""

# Chart 7: Component 6 Management Dashboard
CHART_7 = COMMON_HEADER + """
    subgraph cluster_lark {
        label="Component 6: Management Operations Dashboard (Lark Base)";
        style="rounded,filled";
        fillcolor="#f8fafc";
        color="#cbd5e1";

        db_sync [label="PostgreSQL Bi-directional Sync\\n(FastAPI Outbox REST Sync)", fillcolor="#e0f2fe", color="#0284c7"];
        lark_base [label="Lark Base Master Collaborative Database\\n(Hosted on Bytedance Lark Cloud)", fillcolor="#ede9fe", color="#7c3aed"];

        view1 [label="View 1: Admin Master Grid\\n• Real-time Driver Phone Numbers & Revenue\\n• AI Audit Metadata, Serial Numbers, GPS Map\\n• 1-Click Export to CSV / Microsoft Excel", fillcolor="#ffffff", color="#64748b"];
        view2 [label="View 2: QC Supervisor Kanban\\n• Columns: Đang xử lý | Đã duyệt | Từ chối | Khiếu nại\\n• Inspect Full-Resolution WebP Photos & Finger Signatures\\n• 1-Click Manual Overrides for Edge-Case Appeals", fillcolor="#ffffff", color="#64748b"];
        view3 [label="View 3: Telesales Workspace\\n• Auto-Filtered: status == APPROVED AND phone != EMPTY\\n• Shows Driver Name, Phone Number, Vehicle Model\\n• Interactive Call Status: Chưa gọi | Đã liên hệ | Bận\\n• Notes & Follow-up Survey Dispatch", fillcolor="#ffffff", color="#64748b"];

        pc_admin [label="Accessed on Company PCs via Web / Desktop App\\n(Granular RBAC: Telesales cannot delete or view unapproved rows)", fillcolor="#dcfce7", color="#16a34a"];
    }

    db_sync -> lark_base;
    lark_base -> view1;
    lark_base -> view2;
    lark_base -> view3;
    view1 -> pc_admin;
    view2 -> pc_admin;
    view3 -> pc_admin;
}
"""

CHARTS = [
    ("01_abstract_overall_flow", CHART_1),
    ("02_component_1_ingress_auth", CHART_2),
    ("03_component_2_media_normalization_quality", CHART_3),
    ("04_component_3_fleet_anti_fraud_dual_ai", CHART_4),
    ("05_component_4_persistence_customer_delivery", CHART_5),
    ("06_component_5_telegram_driver_ux_moderation", CHART_6),
    ("07_component_6_management_dashboard", CHART_7),
]

for name, dot_src in CHARTS:
    dot_file = os.path.join(OUTPUT_DIR, f"{name}.dot")
    png_file = os.path.join(OUTPUT_DIR, f"{name}.png")
    jpg_file = os.path.join(OUTPUT_DIR, f"{name}.jpg")

    with open(dot_file, "w", encoding="utf-8") as f:
        f.write(dot_src)

    # 1. render via Graphviz dot to high-res PNG
    subprocess.run(["dot", "-Tpng", dot_file, "-o", png_file], check=True)

    # 2. convert PNG to crisp JPG with pure white background and 95 quality via ImageMagick
    subprocess.run([
        "magick", png_file,
        "-background", "white",
        "-flatten",
        "-quality", "95",
        jpg_file
    ], check=True)

    # clean intermediate PNG
    if os.path.exists(png_file):
        os.remove(png_file)

    print(f"rendered {jpg_file}")
