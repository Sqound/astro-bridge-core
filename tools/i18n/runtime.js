  const storageKey = "astro-bridge.locale";
  let locale = "en";
  try { const saved=localStorage.getItem(storageKey); if(locales.some(l=>l.tag===saved)) locale=saved; }
  catch (_) { /* Local storage is optional. */ }
  // One language at a time: English is built in, any other catalog is its own file.
  const revision=document.querySelector('meta[name="bridge-shell-revision"]')?.content||"";
  const pending=new Map();
  function catalogUrl(tag){return "/i18n-"+tag+".js"+(/^[0-9a-f]{64}$/.test(revision)?"?v="+revision:"");}
  function addCatalog(tag,values){
    if(!locales.some(l=>l.tag===tag)||tag==="en"||!values||typeof values!=="object")return;
    catalogs[tag]=values;
  }
  function loadCatalog(tag){
    if(catalogs[tag])return Promise.resolve(true);
    if(pending.has(tag))return pending.get(tag);
    const promise=new Promise(resolve=>{
      const script=document.createElement("script");script.src=catalogUrl(tag);script.async=true;
      script.onload=()=>resolve(!!catalogs[tag]);script.onerror=()=>{pending.delete(tag);resolve(false);};
      document.head.append(script);
    });
    pending.set(tag,promise);return promise;
  }
  // Parse-time load keeps the first page in the chosen language without a flash of English.
  if(locale!=="en"&&document.readyState==="loading")document.write('<script src="'+catalogUrl(locale)+'"><\/script>');
  function t(key, values={}) {
    const text = catalogs[locale]?.[key] ?? catalogs.en[key] ?? key;
    return text.replace(/\{([a-zA-Z][a-zA-Z0-9_]*)\}/g, (match,name) => Object.hasOwn(values,name) ? String(values[name]) : match);
  }
  function sourceKey(text) {
    if(Object.hasOwn(catalogs.en,text))return text;
    for(const values of Object.values(catalogs))for(const [key,value] of Object.entries(values))if(value===text)return key;
    return null;
  }
  function translate(root=document) {
    root.querySelectorAll("[data-i18n]").forEach(element => {element.textContent=t(element.dataset.i18n);});
    for(const attribute of ["aria-label", "placeholder"])
      root.querySelectorAll("[data-i18n-"+attribute+"]").forEach(element => element.setAttribute(attribute,t(element.getAttribute("data-i18n-"+attribute))));
  }
  function apply() {
    const selected=locales.find(l=>l.tag===locale);
    document.documentElement.lang=selected.complete?locale:"en";
    document.documentElement.dataset.locale=locale;
    translate();
    const current=document.getElementById("locale-current");
    if(!current)return;
    current.textContent=selected.short;
    document.getElementById("locale-select").value=locale;
    document.getElementById("locale-fallback").hidden=selected.complete;
    document.querySelectorAll("button[data-locale]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.locale===locale)));
    document.dispatchEvent(new CustomEvent("bridge:localechange", {detail:{locale}}));
  }
  async function setLocale(tag) {
    if(!locales.some(l=>l.tag===tag))return;
    if(tag!=="en"&&!await loadCatalog(tag))return;
    locale=tag;
    try {localStorage.setItem(storageKey,locale);} catch (_) { /* Session-only preference. */ }
    apply();
  }
  window.BridgeI18n={t,translate,sourceKey,setLocale,addCatalog,loadCatalog,locales,catalogs,get locale(){return locale;}};
  document.documentElement.lang=locales.find(l=>l.tag===locale).complete?locale:"en";
  document.addEventListener("DOMContentLoaded",()=>{
    const options=document.getElementById("locale-options"), select=document.getElementById("locale-select"),menu=document.getElementById("locale-menu");
    for(const language of locales) {
      const button=document.createElement("button");button.type="button";button.dataset.locale=language.tag;
      const name=document.createElement("span");name.lang=language.tag;name.textContent=language.name;
      const code=document.createElement("small");code.textContent=language.complete?language.short:language.short+" · EN";
      button.append(name,code);
      button.addEventListener("click",()=>{setLocale(language.tag);menu.open=false;menu.querySelector("summary").focus();});
      options.append(button);
      const option=document.createElement("option");option.value=language.tag;option.lang=language.tag;
      option.textContent=language.name+(language.complete?"":" · English");select.append(option);
    }
    select.addEventListener("change",()=>setLocale(select.value));
    document.addEventListener("click",event=>{if(!menu.contains(event.target))menu.open=false;});
    menu.addEventListener("keydown",event=>{if(event.key==="Escape"){menu.open=false;menu.querySelector("summary").focus();}});
    window.addEventListener("storage",event=>{
      if(event.key!==storageKey && event.key!==null)return;
      const tag=locales.some(l=>l.tag===event.newValue)?event.newValue:"en";
      void loadCatalog(tag).then(()=>{locale=catalogs[tag]?tag:"en";apply();});
    });
    // A parse-time catalog that failed to load falls back to English, then retries.
    if(!catalogs[locale]){const wanted=locale;locale="en";void loadCatalog(wanted).then(ok=>{if(ok){locale=wanted;apply();}});}
    apply();
  });
})();
