'use strict';

const assert = require('node:assert/strict');
const { existsSync, globSync, readFileSync } = require('node:fs');
const { homedir } = require('node:os');
const { join } = require('node:path');
const { after, before, test } = require('node:test');
const { chromium } = require('playwright');

const ORIGIN = 'https://wave3-smoke.test';
const FORM_ID = 'temp_wave3_smoke';
const FORM_PATH = `/forms/${FORM_ID}`;
const SUBMIT_PATH = '/api/v1/survey/submit-webrtc';
const FIRST_ID = 'VN-FIRST-COMMITTED';
const PHONE = '0900000000';
const NOTE = 'Original trip';
// Two different, valid 2x2 lossless WebP images with the same length.
const RED = Buffer.from('UklGRhwAAABXRUJQVlA4TA8AAAAvAUAAAAcQ/Y/+ByKi/wEA', 'base64');
const BLUE = Buffer.from('UklGRhwAAABXRUJQVlA4TA8AAAAvAUAAAAcQ0f/+ByKi/wEA', 'base64');
const TEMPLATE = readFileSync(join(__dirname, '../static/ui/dynamic_form.html'), 'utf8');

// The server supplies these controls as a template substitution for test forms.
const TEST_BANNER = `<div id="testModeBanner" class="test-banner">
    <span id="txtModeLabel"></span><b id="testModeStatus"></b>
    <label id="bypassCheckLabel">
        <input type="checkbox" id="bypassImgToggle" onchange="toggleBypassImg(this.checked)">
        <span id="txtBypassLabel"></span>
    </label>
    <button type="button" id="btnToggleTestMode" onclick="toggleTestMode()"></button>
</div>`;

function renderForm(testMode) {
    const schema = {
        form_id: FORM_ID,
        title: 'Wave 3 browser smoke',
        description: '',
        settings: { is_test_mode: testMode },
        questions: [
            { id: 'driver_phone', title: 'Phone', type: 'text', required: true },
            { id: 'trip_note', title: 'Trip note', type: 'text', required: true },
            {
                id: 'proof_camera', title: 'Proof photo', type: 'camera_webrtc', required: true,
                image_slots: [{ title: 'Receipt', required: true, allow_upload: true }],
            },
        ],
    };
    return TEMPLATE.replaceAll('{escaped_title}', schema.title)
        // Speculative connections do not go through Playwright request routing.
        .replace(/^[ \t]*<link rel="preconnect"[^>]*>$/gm, '')
        .replaceAll('{escaped_desc}', schema.description)
        .replace('{test_banner_html}', testMode ? TEST_BANNER : '')
        .replace('{schema_json}', JSON.stringify(schema))
        .replace('{locked_ctv}', '')
        .replace('{active_ctv_slug}', '')
        .replace('{active_ref}', '')
        .replaceAll('{tg_bot}', 'SmokeBot')
        .replace("{'flex' if tg_bot else 'none'}", 'flex');
}

function chromiumExecutable() {
    const override = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE;
    if (override && existsSync(override)) return override;
    const cwd = join(homedir(), '.cache/ms-playwright');
    for (const pattern of [
        'chromium-*/chrome-linux*/chrome',
        'chromium_headless_shell-*/chrome-linux*/chrome-headless-shell',
        'chromium_headless_shell-*/chrome-headless-shell-linux*/chrome-headless-shell',
    ]) {
        const path = globSync(pattern, { cwd }).sort().at(-1);
        if (path) return join(cwd, path);
    }
    return undefined;
}

let browser;
before(async () => { browser = await chromium.launch({ executablePath: chromiumExecutable() }); });
after(async () => { await browser?.close(); });

function scenario(name, run, { testMode = false, loseFirst = false } = {}) {
    test(name, { timeout: 15_000 }, async (t) => {
        const context = await browser.newContext({ serviceWorkers: 'block' });
        t.after(() => context.close());
        const html = renderForm(testMode);
        const posts = [];
        const committed = new Map();
        const errors = [];
        await context.route('**/*', async (route) => {
            const request = route.request();
            const url = new URL(request.url());
            if (url.origin === ORIGIN && url.pathname === FORM_PATH && request.isNavigationRequest()) {
                return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
            }
            if (url.origin === ORIGIN && url.pathname === SUBMIT_PATH) {
                const post = { method: request.method() };
                const first = posts.length === 0;
                posts.push(post);
                try {
                    post.body = await new Response(request.postDataBuffer(), {
                        headers: { 'Content-Type': request.headers()['content-type'] },
                    }).formData();
                } catch (error) {
                    errors.push(error);
                    return route.abort();
                }
                const key = post.body.get('idempotency_key');
                if (!committed.has(key)) {
                    committed.set(key, committed.size === 0 ? FIRST_ID : `VN-COMMITTED-${committed.size + 1}`);
                }
                // Simulate a commit whose response never reaches the browser.
                if (loseFirst && first) return route.abort('connectionreset');
                return route.fulfill({ json: {
                    submission_id: committed.get(key), status: 'APPROVED',
                    updated_at: '2020-01-01T00:00:00Z',
                } });
            }
            if (['fetch', 'xhr'].includes(request.resourceType())) errors.push(`Unexpected request: ${request.url()}`);
            return route.abort();
        });
        const page = await context.newPage();
        page.setDefaultTimeout(5_000);
        page.on('pageerror', (error) => errors.push(error));
        const dialogs = [];
        let acceptDialog = false;
        page.on('dialog', async (dialog) => {
            dialogs.push({ type: dialog.type(), message: dialog.message() });
            if (acceptDialog) await dialog.accept();
            else await dialog.dismiss();
        });
        async function expectDialog(action, pattern, { accept = false, type = 'confirm' } = {}) {
            acceptDialog = accept;
            try {
                await action();
            } finally {
                acceptDialog = false;
            }
            assert.equal(dialogs.length, 1, `Expected one ${type}: ${pattern}`);
            const dialog = dialogs.shift();
            assert.equal(dialog.type, type);
            assert.match(dialog.message, pattern);
        }
        // Query coordinates take the real GPS success path without a browser prompt.
        await page.goto(`${ORIGIN}${FORM_PATH}?lat=10.25&lon=106.5`);
        await run({ page, posts, expectDialog });
        assert.deepEqual(dialogs, [], 'Unexpected browser dialogs');
        assert.deepEqual(errors, [], 'Unexpected requests or browser errors');
    });
}

const noteInput = (page) => page.locator('#body_trip_note input');
async function fillForm(page, photo = RED) {
    await page.locator('#body_driver_phone input').fill(PHONE);
    await noteInput(page).fill(NOTE);
    if (photo) await setPhoto(page, photo);
}

function setPhoto(page, bytes) {
    // Start at the real camera slot's post-encoding boundary, avoiding hardware.
    return page.evaluate((data) => {
        applyFileToSlot('proof_camera', 0, new Blob([new Uint8Array(data)], { type: 'image/webp' }));
    }, [...bytes]);
}

async function expectPost(post, { note = NOTE, photo = RED, testMode = false, bypass = false } = {}) {
    assert.ok(post?.body, 'Expected a valid multipart submission');
    assert.equal(post.method, 'POST');
    const fields = [...post.body].filter(([, value]) => typeof value === 'string');
    const key = post.body.get('idempotency_key');
    assert.match(key, /^IDEMP-[0-9A-F]{32}$/);
    assert.deepEqual(fields.sort(), [
        ['driver_phone', PHONE], ['driver_phone', PHONE], ['trip_note', note],
        ['proof_camera_0', 'uploaded_0'], ['is_test_mode', String(testMode)],
        ['form_id', FORM_ID], ['idempotency_key', key], ['bypass_img_check', String(bypass)],
        ['gps_latitude', '10.25'], ['gps_longitude', '106.5'],
    ].sort());
    const files = [...post.body].filter(([, value]) => typeof value !== 'string');
    assert.equal(files.length, 1);
    const [[field, file]] = files;
    assert.equal(field, 'proof_camera__file_0');
    assert.equal(file.name, 'proof_camera_slot_0.webp');
    assert.equal(file.type, 'image/webp');
    assert.deepEqual(Buffer.from(await file.arrayBuffer()), photo);
    return key;
}

function attempt(page) {
    return page.evaluate((id) => ({
        key: clientSubmissionId,
        pendingKey: pendingAttempt?.key ?? null,
        storageKey: localStorage.getItem(`tb_attempt_${id}`),
        meta: JSON.parse(localStorage.getItem(`tb_attempt_meta_${id}`)),
    }), FORM_ID);
}

function expectAttempt(page, key, unresolved, pendingKey = null) {
    return attempt(page).then((state) => assert.deepEqual(state, {
        key, storageKey: key, pendingKey, meta: { key, unresolved, form_id: FORM_ID },
    }));
}

// Await the real handler directly where we need to prove a canceled attempt has settled.
const submit = (page) => page.evaluate(() => handleSubmit());
async function expectResult(page, id) {
    await page.locator('#submissionResultModal').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#subResultId').textContent(), `#${id}`);
    assert.ok(await page.locator('#btnSubmitForm').isEnabled());
}

async function loseResponse(page, posts, options) {
    await fillForm(page);
    await page.locator('#btnSubmitForm').click();
    await expectResult(page, 'NET-ERR');
    assert.equal(posts.length, 1);
    const key = await expectPost(posts[0], options);
    await expectAttempt(page, key, true, key);
    await page.locator('#btnSubResultClose').click();
    return key;
}

scenario('lost response retries the same multipart payload and shows the first committed ID', async ({ page, posts }) => {
    const key = await loseResponse(page, posts);
    await submit(page);
    await expectResult(page, FIRST_ID);
    assert.equal(await page.locator('#submissionStatusId').textContent(), `#${FIRST_ID}`);
    assert.equal(posts.length, 2);
    assert.equal(await expectPost(posts[1]), key);

    const rotated = (await attempt(page)).key;
    assert.notEqual(rotated, key);
    await expectAttempt(page, rotated, false);
    await page.locator('#btnSubResultClose').click();
    await submit(page);
    await expectResult(page, 'VN-COMMITTED-2');
    assert.equal(posts.length, 3);
    assert.equal(await expectPost(posts[2]), rotated);
}, { loseFirst: true });

scenario('reload retains an unresolved key and requires confirmation before sending again', async ({ page, posts, expectDialog }) => {
    const key = await loseResponse(page, posts);
    await page.reload();
    await expectAttempt(page, key, true);
    assert.equal(await page.locator('#slotsBar_proof_camera .captured').count(), 0);
    await fillForm(page);

    const warning = /ảnh gốc không còn/;
    await expectDialog(() => submit(page), warning);
    assert.equal(posts.length, 1);
    await expectAttempt(page, key, true);
    await expectDialog(() => submit(page), warning, { accept: true });
    await expectResult(page, 'VN-COMMITTED-2');
    assert.equal(posts.length, 2);
    assert.notEqual(await expectPost(posts[1]), key);
}, { loseFirst: true });

for (const change of ['changed text', 'same-size/type Blob replacement']) {
    scenario(`${change} requires explicit confirmation for a separate attempt`, async ({ page, posts, expectDialog }) => {
        const key = await loseResponse(page, posts);
        const expected = {};
        if (change === 'changed text') {
            expected.note = 'Revised trip';
            await noteInput(page).fill(expected.note);
        } else {
            assert.equal(BLUE.length, RED.length);
            assert.notDeepEqual(BLUE, RED);
            expected.photo = BLUE;
            await setPhoto(page, BLUE);
        }
        const warning = /Nội dung đã thay đổi/;
        await expectDialog(() => submit(page), warning);
        assert.equal(posts.length, 1);
        await expectAttempt(page, key, true, key);
        await expectDialog(() => submit(page), warning, { accept: true });
        await expectResult(page, 'VN-COMMITTED-2');
        assert.equal(posts.length, 2);
        assert.notEqual(await expectPost(posts[1], expected), key);
    }, { loseFirst: true });
}

for (const cancel of ['reset', 'bypass']) {
    const testMode = cancel === 'bypass';
    scenario(`canceling ${cancel} preserves the unresolved attempt and its payload`, async ({ page, posts, expectDialog }) => {
        const key = await loseResponse(page, posts, { testMode });
        if (cancel === 'reset') {
            await page.getByRole('button', { name: 'Reset', exact: true }).click();
            await expectDialog(() => page.locator('#btnResetConfirm').click(), /bỏ qua lượt cũ/);
            if (await page.locator('#confirmResetModal').isVisible()) await page.locator('#btnResetCancel').click();
        } else {
            const checkbox = page.locator('#bypassImgToggle');
            await expectDialog(() => checkbox.click(), /Thay đổi chế độ ảnh/);
            assert.equal(await checkbox.isChecked(), false);
        }
        assert.equal(posts.length, 1);
        assert.equal(await noteInput(page).inputValue(), NOTE);
        assert.equal(await page.locator('#slotsBar_proof_camera .captured').count(), 1);
        await expectAttempt(page, key, true, key);
        await submit(page);
        await expectResult(page, FIRST_ID);
        assert.equal(posts.length, 2);
        assert.equal(await expectPost(posts[1], { testMode }), key);
    }, { testMode, loseFirst: true });
}

function preparation(page) {
    return page.evaluate(() => ({
        active: bypassPreparationActive,
        slot: Object.hasOwn(answers, 'proof_camera_0'),
        list: Object.hasOwn(answers, 'proof_camera'),
    }));
}

async function pauseAndSubmit(page, posts) {
    await fillForm(page, null);
    await page.locator('#bypassImgToggle').check();
    // Gate the native encoder rather than guessing when a delay has elapsed.
    const queued = await page.evaluate(() => {
        const nativeToBlob = HTMLCanvasElement.prototype.toBlob;
        const encodes = [];
        window.wave3Encodes = encodes;
        HTMLCanvasElement.prototype.toBlob = function (...args) {
            encodes.push(() => nativeToBlob.apply(this, args));
        };
        window.wave3Release = () => {
            HTMLCanvasElement.prototype.toBlob = nativeToBlob;
            encodes.splice(0).forEach((encode) => encode());
        };
        window.wave3FirstSubmit = handleSubmit();
        return encodes.length;
    });
    assert.equal(queued, 1, 'The required custom slot should trigger one encode');
    assert.deepEqual(await preparation(page), { active: true, slot: false, list: false });
    assert.equal(posts.length, 0, 'Do not submit before the encoder finishes');
}

function releaseEncoder(page) {
    return page.evaluate(async () => {
        window.wave3Release();
        await Promise.all([window.wave3FirstSubmit, window.wave3SecondSubmit]);
    });
}

scenario('repeated submit while the canvas encoder is held is single-flight', async ({ page, posts }) => {
    await pauseAndSubmit(page, posts);
    const queued = await page.evaluate(() => {
        window.wave3SecondSubmit = handleSubmit();
        return window.wave3Encodes.length;
    });
    assert.equal(queued, 1, 'A second submit must not start another encode');
    assert.deepEqual(await preparation(page), { active: true, slot: false, list: false });
    assert.equal(posts.length, 0);

    await releaseEncoder(page);
    await expectResult(page, FIRST_ID);
    assert.equal(posts.length, 1);
    assert.deepEqual(await preparation(page), { active: false, slot: true, list: true });
    const photo = Buffer.from(await page.evaluate(async () => {
        const blob = answers.proof_camera_0;
        if (!blob?.isDummy) throw new Error('Expected a generated dummy Blob');
        return [...new Uint8Array(await blob.arrayBuffer())];
    }));
    assert.equal(photo.toString('ascii', 0, 4), 'RIFF');
    assert.equal(photo.toString('ascii', 8, 12), 'WEBP');
    await expectPost(posts[0], { testMode: true, bypass: true, photo });
}, { testMode: true });

for (const interrupt of ['mode switch', 'reset']) {
    scenario(`${interrupt} during encoding discards the late result and sends nothing`, async ({ page, posts, expectDialog }) => {
        await pauseAndSubmit(page, posts);
        const key = (await attempt(page)).key;
        let expectedKey = key;
        if (interrupt === 'mode switch') {
            await page.locator('#btnToggleTestMode').click();
            assert.equal(await page.locator('#bypassCheckLabel').isVisible(), false);
        } else {
            await page.getByRole('button', { name: 'Reset', exact: true }).click();
            await expectDialog(() => page.locator('#btnResetConfirm').click(), /đã được làm lại từ đầu/, { type: 'alert' });
            assert.equal(await noteInput(page).inputValue(), '');
            expectedKey = (await attempt(page)).key;
            assert.notEqual(expectedKey, key);
        }
        await releaseEncoder(page);
        assert.equal(posts.length, 0);
        assert.deepEqual(await preparation(page), { active: false, slot: false, list: false });
        assert.equal(await page.locator('#slotsBar_proof_camera .captured').count(), 0);
        assert.equal(await page.locator('#submissionResultModal').isVisible(), false);
        await expectAttempt(page, expectedKey, false);
    }, { testMode: true });
}
