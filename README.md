# Alertas Zcode Contrarian por Telegram (GitHub Actions + cron-job.org)

cron-job.org llama a GitHub cada 5 minutos. GitHub ejecuta `captura_v2.py` una vez
(captura, alertas del consenso, alerta de ALL Cash >= 60% y, a las 21:20 de Bogota,
el resumen de los partidos de manana) y se apaga.

## 1. Subir el proyecto a GitHub

1. Crea un repositorio nuevo. **Publico** recomendado: las Actions son gratis sin limite.
   En uno privado el plan gratuito da 2.000 min/mes y una corrida cada 5 min gasta ~8.600.
2. Sube todo el contenido de esta carpeta (incluida `.github/workflows/captura.yml`) a la
   rama principal (`main`). El workflow debe estar en la rama por defecto.

## 2. Secrets del repositorio

Settings -> Secrets and variables -> Actions -> New repository secret:

| Nombre     | Valor                                   |
|------------|-----------------------------------------|
| `TG_TOKEN` | token NUEVO del bot (BotFather, `/revoke` el anterior) |
| `TG_CHAT`  | `-1003887899315`                        |

No pegues el token en `captura_v2.py` si el repositorio es publico.

## 3. Probar a mano

Pestana **Actions** -> `captura-zcode` -> **Run workflow**:

1. modo `test`: debe llegarte "Prueba del bot Zcode" a Telegram.
2. modo `captura`: revisa en el log la linea con partidos y ligas. Si Zcode bloquea
   las IP de GitHub, aqui fallara (error de JSON o de conexion).
3. modo `resumen`: envia ya el resumen de manana.

## 4. Token de GitHub para cron-job.org

GitHub -> Settings -> Developer settings -> Personal access tokens -> Fine-grained tokens:

- Repository access: solo este repositorio
- Permissions -> Repository permissions -> **Contents: Read and write**
- Expiracion: la maxima que permita (cuando venza, deja de disparar).

## 5. Trabajo en cron-job.org

Create cronjob:

- **URL:** `https://api.github.com/repos/TU_USUARIO/TU_REPO/dispatches`
- **Schedule:** cada 5 minutos (Every 5 minutes), zona horaria America/Bogota
- Advanced:
  - **Request method:** POST
  - **Headers:**
    - `Authorization: Bearer TU_TOKEN_DE_GITHUB`
    - `Accept: application/vnd.github+json`
    - `Content-Type: application/json`
    - `User-Agent: cronjob`
  - **Request body:** `{"event_type":"captura"}`
- GitHub responde `204` cuando acepta el evento. Eso cuenta como exito.

El resumen de las 21:20 sale en la corrida de las 21:20 (maximo 12 minutos de tolerancia).

## Ajustes en `captura_v2.py`

| Variable | Para que sirve |
|---|---|
| `VENTANA_MIN` | minutos antes del inicio en que se alerta el consenso (20) |
| `ALERTA_CASH_PCT` | umbral de ALL Cash para la alerta de dinero (60) |
| `ALERTA_CASH_VENTANA_MIN` | minutos antes del inicio para esa alerta (20) |
| `MIN_SCORE` | puntaje minimo del consenso (0 = alertar todo consenso) |
| `HORA_RESUMEN` | hora del resumen diario, hora de Bogota (21:20) |

## Notas

- El estado (`estado/`) guarda solo que alertas ya se enviaron, en la cache de Actions.
  Si la cache se pierde pueden repetirse alertas de los partidos de esa ventana.
- Con cron-job.org en 5 minutos y una ventana de 20, cada partido se revisa varias veces antes
  del inicio; si una corrida falla o se retrasa, la siguiente lo cubre.
- Una corrida dura ~30 s. GitHub puede tardar unos segundos en arrancarla.
