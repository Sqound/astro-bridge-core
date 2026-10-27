"""Complete offline notification templates; English is the fallback locale."""
KEYS=('connected','disconnected','test','detected','switch_started','usual','switch_finished',
      'switch_failed','hotspot','mode','update_available','update_installed','boot','failed',
      'private','hotspot_isolated','hotspot_shared','inactive')
_ROWS={
'en':'''{name} connected.
{name} disconnected.
Notifications are enabled.
Wi-Fi detected for {name}.
Switching to {name}.
Usual time for this saved telescope: {usual} s.
Switched to {name} in {seconds} s.
Could not switch to {name} ({seconds} s). Check Astro Bridge.
{name} joined the hotspot.
Connection mode: {mode}.
Update {version} is available.
Update {version} is installed.
Bridge started. Hotspot: {hotspot}. Mode: {mode}.
{name}: connection needs attention.
Private Wi-Fi bridge
Hotspot without Internet
Hotspot with Internet
not active''',
'fr':'''{name} connecté.
{name} déconnecté.
Les notifications sont activées.
Wi-Fi détecté pour {name}.
Basculement vers {name}.
Durée habituelle pour ce télescope enregistré : {usual} s.
Basculement vers {name} terminé en {seconds} s.
Impossible de basculer vers {name} ({seconds} s). Vérifiez Astro Bridge.
{name} a rejoint le hotspot.
Mode de connexion : {mode}.
La mise à jour {version} est disponible.
La mise à jour {version} est installée.
Bridge démarré. Hotspot : {hotspot}. Mode : {mode}.
{name} : la connexion nécessite une intervention.
Bridge sur Wi-Fi privé
Hotspot sans Internet
Hotspot avec Internet
inactif''',
'ja':'''{name} に接続しました。
{name} から切断しました。
通知が有効です。
{name} のWi-Fiを検出しました。
{name} に切替中です。
この保存済み望遠鏡の通常の所要時間：{usual}秒。
{seconds}秒で{name}への切替が完了しました。
{name}に切り替えられませんでした（{seconds}秒）。Astro Bridgeを確認してください。
{name}がホットスポットに接続しました。
接続モード：{mode}。
更新{version}が利用できます。
更新{version}をインストールしました。
ブリッジが起動しました。ホットスポット：{hotspot}。モード：{mode}。
{name}：接続を確認する必要があります。
プライベートWi-Fi経由のブリッジ
インターネットなしのホットスポット
インターネット共有ホットスポット
停止中''',
'de':'''{name} verbunden.
{name} getrennt.
Benachrichtigungen sind aktiviert.
WLAN für {name} erkannt.
Wechsel zu {name} läuft.
Übliche Dauer für dieses gespeicherte Teleskop: {usual} s.
In {seconds} s zu {name} gewechselt.
Wechsel zu {name} fehlgeschlagen ({seconds} s). Prüfen Sie Astro Bridge.
{name} hat sich mit dem Hotspot verbunden.
Verbindungsmodus: {mode}.
Update {version} ist verfügbar.
Update {version} ist installiert.
Bridge gestartet. Hotspot: {hotspot}. Modus: {mode}.
{name}: Die Verbindung erfordert Ihre Aufmerksamkeit.
Bridge im privaten WLAN
Hotspot ohne Internet
Hotspot mit Internet
nicht aktiv''',
'es':'''{name} conectado.
{name} desconectado.
Las notificaciones están activadas.
Wi-Fi detectado para {name}.
Cambiando a {name}.
Tiempo habitual para este telescopio guardado: {usual} s.
Cambio a {name} completado en {seconds} s.
No se pudo cambiar a {name} ({seconds} s). Compruebe Astro Bridge.
{name} se ha unido al punto de acceso.
Modo de conexión: {mode}.
La actualización {version} está disponible.
La actualización {version} está instalada.
Puente iniciado. Punto de acceso: {hotspot}. Modo: {mode}.
{name}: la conexión requiere atención.
Puente en Wi-Fi privado
Punto de acceso sin Internet
Punto de acceso con Internet
inactivo''',
'it':'''{name} connesso.
{name} disconnesso.
Le notifiche sono attive.
Wi-Fi rilevato per {name}.
Passaggio a {name} in corso.
Tempo abituale per questo telescopio salvato: {usual} s.
Passaggio a {name} completato in {seconds} s.
Impossibile passare a {name} ({seconds} s). Controlla Astro Bridge.
{name} si è collegato all’hotspot.
Modalità di connessione: {mode}.
L’aggiornamento {version} è disponibile.
L’aggiornamento {version} è installato.
Bridge avviato. Hotspot: {hotspot}. Modalità: {mode}.
{name}: la connessione richiede attenzione.
Bridge su Wi-Fi privato
Hotspot senza Internet
Hotspot con Internet
inattivo''',
'pt-BR':'''{name} conectado.
{name} desconectado.
As notificações estão ativadas.
Wi-Fi detectado para {name}.
Trocando para {name}.
Tempo habitual deste telescópio salvo: {usual} s.
Troca para {name} concluída em {seconds} s.
Não foi possível trocar para {name} ({seconds} s). Verifique o Astro Bridge.
{name} entrou no ponto de acesso.
Modo de conexão: {mode}.
A atualização {version} está disponível.
A atualização {version} está instalada.
Ponte iniciada. Ponto de acesso: {hotspot}. Modo: {mode}.
{name}: a conexão requer atenção.
Ponte no Wi-Fi privado
Ponto de acesso sem Internet
Ponto de acesso com Internet
inativo''',
'zh-Hans':'''{name} 已连接。
{name} 已断开。
通知已启用。
已检测到 {name} 的 Wi-Fi。
正在切换到 {name}。
此已保存望远镜的通常耗时：{usual} 秒。
已在 {seconds} 秒内切换到 {name}。
无法切换到 {name}（{seconds} 秒）。请检查 Astro Bridge。
{name} 已加入热点。
连接模式：{mode}。
更新 {version} 已可用。
更新 {version} 已安装。
桥接器已启动。热点：{hotspot}。模式：{mode}。
{name}：连接需要处理。
私人 Wi-Fi 桥接
无互联网热点
共享互联网热点
未启用''',
'zh-Hant':'''{name} 已連線。
{name} 已中斷。
通知已啟用。
已偵測到 {name} 的 Wi-Fi。
正在切換至 {name}。
此已儲存望遠鏡的通常所需時間：{usual} 秒。
已在 {seconds} 秒內切換至 {name}。
無法切換至 {name}（{seconds} 秒）。請檢查 Astro Bridge。
{name} 已加入熱點。
連線模式：{mode}。
更新 {version} 已可用。
更新 {version} 已安裝。
橋接器已啟動。熱點：{hotspot}。模式：{mode}。
{name}：連線需要處理。
私人 Wi-Fi 橋接
無網際網路熱點
共享網際網路熱點
未啟用''',
'ru':'''{name}: подключено.
{name}: отключено.
Уведомления включены.
Обнаружен Wi-Fi телескопа {name}.
Переключение на {name}.
Обычное время для этого сохранённого телескопа: {usual} с.
Переключение на {name} завершено за {seconds} с.
Не удалось переключиться на {name} ({seconds} с). Проверьте Astro Bridge.
{name}: подключение к точке доступа.
Режим подключения: {mode}.
Доступно обновление {version}.
Обновление {version} установлено.
Мост запущен. Точка доступа: {hotspot}. Режим: {mode}.
{name}: подключение требует внимания.
Мост через домашний Wi-Fi
Точка доступа без Интернета
Точка доступа с Интернетом
не активна'''
}
TEXT={locale:dict(zip(KEYS,rows.splitlines())) for locale,rows in _ROWS.items()}


def message(locale,code,values):
    text=TEXT.get(locale,TEXT['en']);values=dict(values)
    if 'mode' in values:values['mode']=text.get(values['mode'],text['private'])
    if 'hotspot' in values and not values['hotspot']:values['hotspot']=text['inactive']
    result=text[code].format(**values)
    if code=='switch_started' and 'usual' in values:result+=' '+text['usual'].format(**values)
    return result

# Newly discovered, unsaved devices open their prefilled addition form.
_NEW_DETECTED={
'en':'New telescope Wi-Fi detected: {name}. Tap to add it.',
'fr':'Nouveau Wi-Fi de télescope détecté : {name}. Touchez pour l’ajouter.',
'ja':'新しい望遠鏡のWi-Fiを検出：{name}。タップして追加。',
'de':'Neues Teleskop-WLAN erkannt: {name}. Zum Hinzufügen antippen.',
'es':'Nueva Wi-Fi de telescopio detectada: {name}. Toca para añadirlo.',
'it':'Rilevato un nuovo Wi-Fi del telescopio: {name}. Tocca per aggiungerlo.',
'pt-BR':'Novo Wi-Fi de telescópio detectado: {name}. Toque para adicionar.',
'ru':'Обнаружена новая сеть телескопа: {name}. Нажмите, чтобы добавить.',
'zh-Hans':'发现新的望远镜 Wi-Fi：{name}。轻点以添加。',
'zh-Hant':'發現新的望遠鏡 Wi-Fi：{name}。點一下以加入。'}
for _locale,_template in _NEW_DETECTED.items():TEXT[_locale]['new_detected']=_template
