// Adapted from manishkatyan/bbb-mp4 (MIT, see LICENSE.upstream).
const puppeteer = require('puppeteer');
const child_process = require('child_process');
const Xvfb = require('xvfb');

// Generate randome display port number to avoide xvfb failure
var disp_num = Math.floor(Math.random() * (200 - 99) + 99);
var xvfb = new Xvfb({
    displayNum: disp_num,
    silent: true,
    xvfb_args: ["-screen", "0", "1280x800x24", "-ac", "-nolisten", "tcp", "-dpi", "96", "+extension", "RANDR"]
});
var width = 1280;
var height = 800;
var options = {
    headless: false,
    args: [
        '--disable-infobars',
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--start-fullscreen',
        '--app=https://www.google.com/',
        `--window-size=${width},${height}`,
    ],
}
options.executablePath = "/usr/bin/google-chrome"
async function main() {
    let browser, page;
    try {
        xvfb.startSync()

        var url = process.argv[2];
        if (!url) {
            console.warn('URL undefined!');
            process.exit(1);
        }
        // Validate URL
        var urlRegex = new RegExp('^https?:\\/\\/.*\\/playback\\/presentation\\/2\\.3\\/[a-z0-9]{40}-[0-9]{13}');
        if (!urlRegex.test(url)) {
            console.warn('Invalid recording URL for bbb 2.3!');
            console.warn(url)
            process.exit(1);
        }

        // Set exportname
        var exportname = new URL(url).pathname.split("/")[4]

        // set duration to 0
        var duration = 0

        browser = await puppeteer.launch(options)
        const pages = await browser.pages()

        page = pages[0]

        page.on('console', msg => {
            var m = msg.text();
            console.log('PAGE LOG:', m) // uncomment if you need
        });

        await page._client.send('Emulation.clearDeviceMetricsOverride')
            // Catch URL unreachable error
        await page.goto(url, { waitUntil: 'networkidle2' }).catch(e => {
            console.error('Recording URL unreachable!');
            process.exit(2);
        })
        await page.setBypassCSP(true)

        // Check if recording exists (search "404" message)
        await page.waitForTimeout(5 * 1000)
        try {
            var loadMsg = await page.$eval('.error-code', el => el.textContent);
            console.log(loadMsg)
            if (loadMsg == "404") {
                console.warn("Recording not found!");
                process.exit(1);
            }
        } catch (err) {
            console.log("Recording found")
        }

        // A BBB page may render its controls before the media metadata has
        // arrived. In particular duration is NaN for some long recordings.
        // Never pass NaN to ffmpeg or to a timer: doing so immediately tears
        // down Xvfb. The explicit override is useful if metadata never loads.
        const override = process.env.BBB_MP4_DURATION;
        if (override) {
            duration = Number(override);
            if (!Number.isFinite(duration) || duration <= 0 || duration > 604800)
                throw new Error('Invalid duration override');
            console.log('Using explicit duration override (seconds): ' + duration);
        } else {
            try {
                await page.waitForFunction(() => {
                    const media = document.getElementById('vjs_video_3_html5_api');
                    return !!media && Number.isFinite(media.duration) && media.duration > 0 && media.duration <= 604800;
                }, {timeout: 120000, polling: 500});
                duration = await page.evaluate(() => document.getElementById('vjs_video_3_html5_api').duration);
            } catch (err) {
                throw new Error('Не удалось получить длительность записи за 120 с. Укажите длительность вручную в параметрах задания.');
            }
        }
        console.log(duration);

        await page.waitForSelector('button[class=vjs-big-play-button]');
        await page.$eval('.bottom-content', element => element.style.display = "none");
        await page.$eval('.fullscreen-button', element => element.style.opacity = "0");
        await page.$eval('.right', element => element.style.opacity = "0");
        await page.$eval('.vjs-control-bar', element => element.style.opacity = "0");
        await page.click('button[class=vjs-big-play-button]', { waitUntil: 'domcontentloaded' });

        // Keep the browser and Xvfb alive until FFmpeg actually finishes.
        // FFmpeg itself controls the capture duration, and reports failures.
        await new Promise((resolve, reject) => {
            const capture = child_process.spawn('sh', ['ffmpeg-cmd.sh',
                String(duration), exportname, String(disp_num)]);
            capture.stdout.on('data', data => console.log(`stdout: ${data}`));
            capture.stderr.on('data', data => console.error(`stderr: ${data}`));
            capture.on('error', reject);
            capture.on('close', code => {
                console.log(`child process exited with code ${code}`);
                if (code === 0) resolve();
                else reject(new Error('FFmpeg завершился с кодом ' + code));
            });
        });

    } catch (err) {
        console.error(err)
        process.exitCode = 1
    } finally {
        if (page) await page.close().catch(() => {})
        if (browser) await browser.close().catch(() => {})
        try { xvfb.stopSync() } catch (err) { console.error(err) }
    }
}

main()