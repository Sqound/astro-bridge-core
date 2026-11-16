## Sessions et historique local

Les cartes des instruments connectés présentent les données de session activées,
en lecture seule : opération, cible, valeurs avec unités et ancienneté. Après trois
périodes de la source, une valeur porte l’heure de sa dernière lecture. « En pause »
signifie que l’observateur a cédé la place, pas que l’instrument s’est arrêté. Les
valeurs absentes restent inconnues. Choisissez les données dans chaque profil.
L’empilement Seestar nécessite une clé fournie par le propriétaire ; son obtention
exige une action explicite et Internet sur le bridge. Vos choix d’écoute sont
conservés lors du remplacement de la clé. DWARF reste limité à la liaison et à une
seule application à la fois.

Ouvrir la session affiche les graphiques conservés, les maxima d’images, les rejets
et l’environnement. La durée de pose observée compte seulement les augmentations
vues d’images conservées à durée de pose inchangée. Un point de départ absent,
un changement ou une remise à zéro rend le total partiel ; la dernière durée par
image reste distincte. Les hachures signalent l’absence de téléchargement de données
brutes enregistré par une application jumelée, pas qu’une application était fermée.
La couverture des journaux anciens ou agrégés reste inconnue. Une horloge non
vérifiée donne des horaires approximatifs ; l’ordre des séquences reste valable.
Les événements récents sont limités à 500 mesures et enregistrements de cycle de
vie. Les exports CSV/JSON contiennent toutes les mesures conservées ; le JSON
inclut aussi les événements et noms des lecteurs. Configuration → Sessions règle
la conservation en nuits et Mio ; la première limite atteinte s’applique. Réduire
une limite peut effacer des données anciennes. La suppression est irréversible :
exportez d’abord, puis confirmez. Seules les applications jumelées autorisées à la
télémétrie ou à l’administration peuvent lire les sessions, sans contrôler l’instrument.
L’historique reste local même sans application lectrice.

## Caméras branchées et limites

Les Pi Zero W et Zero 2 W servent uniquement aux connexions et à la télémétrie.
Les caméras visent les Pi de classe 3/4 approuvés ; commencez leur validation
physique sur un banc Pi 3B. Activez volontairement le partage et la découverte dans
Configuration → Caméras. La démo démarre sans caméra ; son bouton ajoute seulement
un simulateur local, jamais une caméra physique. Les clients découvrent le bridge
et lui demandent ses adresses réelles par connexion, sans vous faire saisir
l’adresse d’un instrument. La découverte Alpaca répond seulement sur les réseaux
de gestion confirmés ; une adresse n’apparaît qu’après l’ouverture de son écouteur.

Seules les caméras compatibles physiquement branchées sont partagées. UVC exige
un format compatible et l’étalonnage du pas des pixels par le propriétaire via
camera-calibration.json ; aucun éditeur d’étalonnage n’est fourni. Suivez la
procédure propriétaire, préservez les autres entrées puis
rebranchez ou redémarrez sans risque. Les identifiants liés au port ne prouvent pas
l’identité physique. UVC acquiert une luminance bornée (GREY/Y16/YUYV), jusqu’à
1280×960, avec regroupement de pixels limité à 1. Les demandes longues moyennent
plusieurs images, sans véritable pose longue continue. Sans contrôle d’exposition,
la durée est nominale. Une caméra sans obturateur ne fabrique pas d’images noires.
Aucun SDK fabricant n’est fourni ; ces caméras exigent un pilote compatible installé.
Le 2.4 GHz peut ralentir les gros transferts. Le regroupement par 2, si pris en
charge, est seulement un réglage initial ; les choix des applications sont conservés.

Un seul client Alpaca possède la caméra à la fois. ClientID est un numéro de
protocole, pas une identité d’application jumelée. Le nom DHCP exige une correspondance
exacte et actuelle de l’adresse. Autoriser cette application à l’utiliser exige votre
accord explicite pour libérer le propriétaire affiché et peut interrompre une pose.
Un changement de propriétaire invalide l’ancien accord. L’autre application doit
se reconnecter ; la libération ne désigne pas automatiquement un nouveau propriétaire.
Des déconnexions répétées invitent à vérifier câble et concentrateur alimenté, sans
prouver une panne de tension. Caméras réelles, téléphones, performances Pi et ConformU
restent **documentés, non vérifiés sur matériel** ; voir les limites USB.
