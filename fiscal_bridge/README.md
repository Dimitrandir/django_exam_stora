# Мост към касата (облачна STORA)

Когато STORA работи в облака, сървърът не вижда касовия апарат в
магазина. Мостът е малка програма на касовия компютър: браузърът на
касиера взима бона от STORA, подава го на моста на `127.0.0.1:7777`, а
мостът пуска ECRCommApp и печата, точно както локалната STORA.

Мостът ползва само стандартния Python, без `pip install`.

## 1. На хостинга (веднъж)

1. Git Version Control → Manage → **Update from Remote**.
2. В `stora/.env` добави или смени:
   ```
   FISCAL_ENABLED=True
   FISCAL_MODE=bridge
   ```
   `FISCAL_COM_PORT`, `FISCAL_ECRCOMMAPP_PATH` и паролата на оператора НЕ
   се пишат тук, те отиват в `bridge.ini` на касовия компютър.
3. Setup Python App → Execute python script: `run.py migrate`, после
   `run.py collectstatic --noinput`.
4. **RESTART**.

## 2. На касовия компютър

Трябват Python 3 и ECRCommApp (на компютрите, където вече върви локална
STORA, и двете ги има).

1. Копирай папката `fiscal_bridge` от проекта, напр. в
   `C:\stora-bridge\fiscal_bridge`. Името на папката трябва да остане
   `fiscal_bridge`.
2. В нея копирай `bridge.ini.example` като `bridge.ini` и попълни:
   - `allowed_origins`: адресът на STORA, напр.
     `https://stora.petarstoychev.com`.
   - `ecrcommapp_path`, `com_port`, `ecr_api_url`: същите стойности като
     `FISCAL_ECRCOMMAPP_PATH`, `FISCAL_COM_PORT`, `FISCAL_API_URL` от `.env`
     на локалната STORA.
   - `operator_num`, `operator_password`: операторът на апарата (като
     `FISCAL_OPERATOR_NUM`/`FISCAL_OPERATOR_PASSWORD`).
3. Пробно пускане в cmd:
   ```
   cd C:\stora-bridge\fiscal_bridge
   python bridge.py
   ```
   Трябва да пише `STORA fiscal bridge listening on http://127.0.0.1:7777`.
   В браузъра отвори `http://127.0.0.1:7777/status`, трябва да видиш
   `"ok": true`.
4. Пробна продажба в облачната STORA (в брой). Chrome може веднъж да
   попита дали сайтът да има достъп до устройства в локалната мрежа: дай
   **Allow**. Горе вдясно излиза „Фискалният бон се печата...“, после
   „Фискалният бон е отпечатан.“.

## 3. Мостът да тръгва сам (NSSM)

Както waitress на локалната инсталация, в cmd като администратор:

```
nssm install STORAFiscalBridge "C:\Path\To\python.exe" "C:\stora-bridge\fiscal_bridge\bridge.py"
nssm set STORAFiscalBridge AppDirectory "C:\stora-bridge\fiscal_bridge"
nssm start STORAFiscalBridge
```

Пътя до python виж с `where python`.

## Как работи и какво става при проблем

- Продажбата винаги се записва първо. Докато бонът не е отпечатан, тя е
  със статус „Чака печат“.
- Ако мостът не върви, касиерът вижда червено съобщение. Продажбата си
  остава; пусни моста и натисни **Retry Fiscal Print** в детайлите на
  продажбата.
- X/Z и отчетите за период от екрана „Fiscal Reports“ също минават през
  моста.
- Мостът слуша само на `127.0.0.1` (не се вижда от други компютри) и
  приема бонове само от адресите в `allowed_origins`.
- Паролата на оператора стои само в `bridge.ini`, не минава през
  браузъра и не се пази в облака.
