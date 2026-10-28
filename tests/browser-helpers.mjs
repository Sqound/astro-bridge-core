/** Use the same account-menu path as a person opening header preferences. */
export async function clickUserControl(page, selector) {
  if(!await page.locator('#user-menu').evaluate(element=>element.open))await page.locator('#user-menu>summary').click();
  await page.locator(selector).click();
}
