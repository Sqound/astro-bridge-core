## Sesiones e historial local

Las tarjetas de instrumentos conectados muestran los datos de sesión activados en
solo lectura: operación, objetivo, valores con unidades y antigüedad. Tras tres
intervalos de la fuente se marca la última lectura. En pausa significa que el
observador cedió acceso, no que el instrumento se detuvo. Los valores ausentes
siguen siendo desconocidos. Elija los datos en cada perfil. El apilado Seestar
necesita una clave proporcionada por el propietario; obtenerla requiere una acción
explícita e Internet en el puente. Las elecciones de escucha se conservan al
cambiar la clave. DWARF solo ofrece enlace y acepta una app a la vez.

Abrir sesión muestra gráficos conservados, máximos de fotogramas, rechazos y entorno.
La exposición observada solo suma aumentos vistos de fotogramas aceptados con una
duración de exposición constante. Valores iniciales ausentes, cambios o reinicios
pueden dejar el total incompleto; la última duración por fotograma va aparte.
El rayado significa que no consta una descarga de datos brutos por una app vinculada,
no que estuviera cerrada. La cobertura de registros antiguos o agregados sigue
siendo desconocida. Los relojes sin verificar dan horas aproximadas; el orden de
secuencia se mantiene. Los eventos recientes se limitan a 500 lecturas y registros
de ciclo de vida. CSV/JSON contienen todas las lecturas conservadas; JSON también
incluye eventos y nombres de lectores. Configuración → Sesiones ajusta noches y
MiB; se aplica el primer límite alcanzado. Reducir límites puede borrar datos antiguos.
La eliminación es irreversible: exporte primero y confirme explícitamente. Solo las
apps vinculadas con permiso de telemetría o administración pueden leer sesiones;
estos permisos nunca controlan el instrumento. El historial permanece local aun sin
una app lectora.

## Cámaras conectadas y límites

Pi Zero W y Zero 2 W son solo para conexiones y telemetría. Las cámaras requieren
un Pi de clase 3/4 aprobado; inicie la aceptación física de cámaras en un banco Pi 3B.
Active explícitamente el servicio y la detección en Configuración → Cámaras. La demo
empieza vacía; su botón de conexión solo añade un simulador local. Los clientes
descubren el puente y solicitan sus endpoints reales por conexión; no es necesario
escribir la dirección del instrumento. La detección Alpaca solo responde en redes
de gestión confirmadas y solo publica direcciones tras abrir el servicio de escucha.

Solo se comparten cámaras compatibles físicamente conectadas. UVC requiere formatos
compatibles y calibración del paso de píxel por el propietario mediante
camera-calibration.json; no hay editor de calibración. Siga el
procedimiento del propietario, conserve las otras entradas y
reconecte o reinicie de forma segura. Los ID basados en puerto no prueban identidad
física. UVC captura luminancia limitada (GREY/Y16/YUYV), hasta 1280×960 con binning 1.
Las solicitudes largas promedian fotogramas; no son exposiciones largas continuas.
Sin controles de exposición, la duración es nominal. Una cámara sin obturador no
inventa fotogramas oscuros. No se incluye ningún SDK del fabricante; estas cámaras
necesitan un controlador compatible instalado. Las transferencias grandes por
2.4 GHz pueden ser lentas. El binning 2 compatible es solo un valor inicial; las
apps conservan sus elecciones.

Un cliente Alpaca posee la cámara a la vez. ClientID es un número de protocolo, no
una app vinculada; el nombre DHCP exige una coincidencia exacta de dirección actual.
Permitir que esta app la use exige consentimiento explícito para liberar al propietario
actual mostrado y puede interrumpir una exposición. Un cambio de propietario invalida
el consentimiento anterior. La otra app debe reconectarse tras la liberación; no
se asigna automáticamente un nuevo propietario. Las desconexiones repetidas sugieren
revisar el cable y usar un concentrador alimentado, no prueban un fallo de tensión.
Cámaras y teléfonos reales, rendimiento Pi y ConformU siguen **documentados, sin
verificar en hardware**; consulte los límites USB.
