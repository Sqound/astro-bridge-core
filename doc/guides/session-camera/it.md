## Sessioni e cronologia locale

Le schede degli strumenti collegati mostrano i dati della sessione attivi in sola
lettura: operazione, obiettivo, valori con unità ed età. Dopo tre intervalli della
sorgente compare l’ultima lettura. In pausa significa che l’osservatore ha ceduto
l’accesso, non che lo strumento si è fermato. I valori mancanti restano sconosciuti.
Scelga i dati in ogni profilo. Lo stacking Seestar richiede una chiave fornita dal
proprietario; ottenerla richiede un’azione esplicita e Internet sul bridge. Le scelte
di ascolto si conservano cambiando chiave. DWARF offre solo collegamento e una app
alla volta.

Apri sessione mostra grafici conservati, massimi dei fotogrammi, scarti e ambiente.
L’esposizione osservata somma solo gli incrementi visti dei fotogrammi accettati a
durata invariata. Valori iniziali mancanti, modifiche o azzeramenti possono rendere
il totale parziale; l’ultima durata per fotogramma è separata. Il tratteggio indica
nessun download di dati grezzi registrato da app associate, non un’app chiusa.
La copertura dei registri vecchi o aggregati resta sconosciuta. Gli orologi non
verificati danno orari approssimativi; l’ordine di sequenza resta valido. Gli eventi
recenti sono limitati a 500 letture e registrazioni del ciclo di vita. CSV/JSON
contengono tutte le letture conservate; JSON include anche eventi e nomi dei lettori.
Configurazione → Sessioni imposta notti e MiB; vale il primo limite raggiunto.
Ridurre i limiti può rimuovere dati vecchi. L’eliminazione è irreversibile: esporti
prima, poi confermi esplicitamente. Solo app associate con permessi di telemetria o
amministrazione possono leggere le sessioni; questi permessi non controllano mai lo
strumento. La cronologia resta locale anche senza app che la leggono.

## Fotocamere collegate e limiti

Pi Zero W e Zero 2 W sono solo per connessioni e telemetria. Le fotocamere richiedono
un Pi di classe 3/4 approvato; inizi la verifica fisica su un banco Pi 3B.
In Configurazione → Fotocamere attivi condivisione e rilevamento esplicitamente.
La demo inizia vuota; il suo pulsante aggiunge solo un simulatore locale. I client
scoprono il bridge e richiedono gli endpoint reali per connessione; non occorre
digitare un indirizzo dello strumento. Il rilevamento Alpaca risponde solo sulle
reti di gestione confermate e pubblica indirizzi solo dopo l’apertura del listener.

Si condividono solo fotocamere supportate fisicamente collegate. UVC richiede formati
supportati e calibrazione del passo dei pixel da parte del proprietario tramite
camera-calibration.json; non esiste un editor di calibrazione. Segua la
procedura del proprietario, conservi le altre voci, poi ricolleghi
o riavvii in sicurezza. Gli ID basati sulla porta non provano l’identità fisica.
UVC acquisisce luminanza limitata (GREY/Y16/YUYV), fino a 1280×960 con binning 1.
Le richieste lunghe fanno la media di fotogrammi, non una vera esposizione lunga
continua. Senza controlli di esposizione, la durata è nominale. Una fotocamera senza
otturatore non inventa immagini scure. Nessun SDK del produttore è incluso; queste
fotocamere richiedono un driver supportato installato. Trasferimenti grandi a
2.4 GHz possono essere lenti. Il binning 2 supportato è solo un valore iniziale;
le app mantengono le proprie scelte.

Un client Alpaca possiede la fotocamera alla volta. ClientID è un numero di protocollo,
non un’app associata; i nomi DHCP appaiono solo con corrispondenza esatta attuale
dell’indirizzo. Consenti a questa app di usarla richiede consenso esplicito per liberare
il proprietario attuale mostrato e può interrompere un’esposizione. Un cambio di
proprietario invalida il consenso precedente. L’altra app deve riconnettersi dopo
il rilascio; non si assegna automaticamente un nuovo proprietario. Scollegamenti
ripetuti suggeriscono di controllare cavo e hub alimentato, senza provare un guasto
di tensione. Fotocamere e telefoni reali, prestazioni Pi e ConformU restano
**documentati, non verificati su hardware**; veda i limiti USB.
