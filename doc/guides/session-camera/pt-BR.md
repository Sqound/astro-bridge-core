## Sessões e histórico local

Os cartões dos instrumentos conectados mostram dados de sessão ativados somente
para leitura: operação, alvo, valores com unidades e idade. Após três intervalos
da fonte, o valor é marcado como última leitura. Pausado significa que o observador
cedeu acesso, não que o instrumento parou. Valores ausentes continuam desconhecidos.
Escolha os dados em cada perfil. O empilhamento Seestar precisa de uma chave
fornecida pelo proprietário; obtê-la exige ação explícita e Internet no bridge.
As escolhas de escuta são mantidas ao trocar a chave. DWARF só oferece enlace e
aceita um app por vez.

Abrir sessão mostra gráficos mantidos, máximos de quadros, rejeições e ambiente.
A exposição observada soma apenas aumentos vistos de quadros aceitos com duração
inalterada. Falta de valores iniciais, mudanças ou reinícios podem deixar o total
parcial; a última duração por quadro fica separada. As hachuras significam ausência
de download registrado de dados brutos por app pareado, não que um app estava
fechado. A cobertura de registros antigos ou agregados continua desconhecida.
Relógios não verificados mostram horários aproximados; a ordem de sequência continua
válida. Eventos recentes são limitados a 500 leituras e registros de ciclo de vida.
CSV/JSON contêm todas as leituras mantidas; JSON também inclui eventos e nomes dos
leitores. Configurações → Sessões define noites e MiB; vale o primeiro limite
atingido. Reduzir limites pode remover dados antigos. Excluir é irreversível:
exporte primeiro, depois confirme explicitamente. Só apps pareados com permissão de
telemetria ou administração podem ler sessões; essas permissões nunca controlam o
instrumento. O histórico fica local mesmo sem um app leitor.

## Câmeras conectadas e limites

Pi Zero W e Zero 2 W são apenas para conexões e telemetria. Câmeras exigem um Pi da
classe 3/4 aprovada; inicie a aceitação física em uma bancada Pi 3B. Em Configurações
→ Câmeras, ative o serviço e a descoberta deliberadamente. A demo começa vazia;
o botão de conexão só adiciona um simulador local. Os clientes descobrem o bridge
e pedem seus endpoints reais por conexão; não é preciso digitar o endereço do
instrumento. A descoberta Alpaca só responde em redes de gerenciamento confirmadas,
e o endereço só aparece após a abertura bem-sucedida do serviço de escuta.

Só câmeras compatíveis fisicamente conectadas são disponibilizadas. UVC precisa de
formatos compatíveis e calibração do passo dos pixels pelo proprietário via
camera-calibration.json; não há editor de calibração. Siga o
procedimento do proprietário, mantenha outras entradas e depois
reconecte ou reinicie com segurança. IDs baseados na porta não comprovam identidade
física. UVC captura luminância limitada (GREY/Y16/YUYV), até 1280×960 com binning 1.
Pedidos longos calculam a média de quadros, não uma exposição longa contínua real.
Sem controles de exposição, a duração é nominal. Câmeras sem obturador não inventam
quadros escuros. Nenhum SDK do fabricante é incluído; essas câmeras precisam de um
driver compatível instalado. Transferências grandes em 2.4 GHz podem ser lentas.
Binning 2 compatível é só o valor inicial; os apps mantêm suas escolhas.

Um cliente Alpaca detém a câmera por vez. ClientID é um número de protocolo, não
um app pareado; nomes DHCP só aparecem com correspondência exata e atual do endereço.
Permitir que este app a use exige consentimento explícito para liberar o proprietário
atual exibido e pode interromper uma exposição. Mudança de proprietário invalida o
consentimento antigo. O outro app deve se reconectar após a liberação; um novo
proprietário não é definido automaticamente. Desconexões repetidas sugerem conferir
cabo e hub alimentado, sem provar falha de tensão. Câmeras e telefones reais,
desempenho Pi e ConformU continuam **documentados, não verificados em hardware**;
veja os limites USB.
