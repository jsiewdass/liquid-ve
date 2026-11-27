# liquidVE

Motor P2P para órdenes USDT/VES. Las compras usan Pago Móvil por BanescOnline
con autorización manual. Las ventas conservan su integración con Cachicamo
para verificar pagos entrantes.

## Entorno local

Requiere Python 3.11 o superior. Configura los valores de `.env.example` en
`.env` y ejecuta:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app
```

Para ejecutar pruebas sin conexiones a Banesco, Binance o Telegram:

```bash
python -m pytest
python -m ruff check app tests scripts alembic
```

## Pago Móvil supervisado

1. Instala Chromium con `python -m playwright install --with-deps chromium`.
   El contenedor admite `docker build --build-arg INSTALL_CHROMIUM=true .`.
2. Configura la sesión Playwright de Binance, las credenciales de Banesco y el
   bot en `.env`. Desde el teléfono abre un chat privado con el bot e inicia la
   conversación. Configura `TELEGRAM_OPERATOR_CHAT_ID` con tu chat.
3. Configura `OPERATOR_PUBLIC_URL` con el origen HTTPS accesible desde tu
   teléfono. Por ejemplo, un origen privado de Tailscale. Ejecuta una sola
   instancia del servicio de pagos y persiste el directorio `data/`.
4. Habilita `BINANCE_BROWSER_ENABLED`, `TELEGRAM_ENABLED` y
   `BANESCO_RAIL_ENABLED`. El worker de compras, si se habilita, solo detecta
   órdenes. Para empezar un pago, llama a
   `POST /api/v1/orders/buy/{order_number}/pay` con el encabezado `X-API-Key`.
5. Cuando llegue el aviso, abre el enlace, revisa monto y destinatario e
   ingresa el código que te entrega Banesco. Tienes hasta 30 segundos para
   ingresarlo desde que abres el formulario. El enlace se puede abrir durante los
   primeros cinco minutos por defecto. Banesco controla la validez del código y de
   la sesión bancaria. liquidVE entrega el código al banco y lo descarta.
6. Consulta `GET /api/v1/orders/buy/{order_number}/payment` con el mismo
   encabezado. `PAYMENT_CONFIRMED` requiere que el importe y la referencia del
   comprobante se hayan verificado y que exista la imagen. Se guarda como
   `<sha256>.png` dentro de `RECEIPT_STORAGE_DIR`. Revisa en el banco cualquier
   operación que quede en `MANUAL_REVIEW` antes de considerar un nuevo pago.

El formato del comprobante se probó con datos sintéticos. El primer pago
real debe ser supervisado y de monto bajo para contrastar el comprobante que
muestra el banco. La subida del comprobante a Binance y la verificación de
la liberación de los USDT quedan pendientes.

Los registros históricos de la base de datos se preservan al actualizar.

## Prueba local contestando al bot de Telegram

Este recorrido se ejecuta desde tu computadora con el script
`scripts/banesco_telegram_payment.py`. El bot solicita una respuesta en el
mismo chat privado. El plazo para responder empieza con el aviso y dura 30
segundos por defecto.

Para verificar el chat con un código de prueba, ejecuta desde `liquid-ve/`:

```bash
python scripts/banesco_telegram_payment.py
```

El script busca `.env` en el repositorio y luego en la carpeta que lo contiene.
Responde al aviso del bot con el número de prueba que aparece en el mensaje.

Para un Pago Móvil real supervisado, ejecuta:

```bash
python scripts/banesco_telegram_payment.py --execute-payment --attempt-id prueba-01 \
  --phone TELEFONO --document DOCUMENTO --bank CODIGO_BANCO --amount MONTO --keep-open
```

Completa `DAILY_PAY_LIMIT_VES` en tu `.env` junto a los límites existentes.
El script espera la respuesta al aviso de ese intento, pasa el código a la misma
sesión de Playwright y guarda el comprobante cuando reconoce la referencia y
el monto. El mensaje que envías queda en el historial del chat. liquidVE
mantiene las reservas del piloto en `data/banesco-telegram-pilot.sqlite3`. Si
el resultado requiere revisión, consulta los movimientos en Banesco y
conserva el identificador del intento.
