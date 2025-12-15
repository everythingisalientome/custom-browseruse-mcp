from cdp_client import ChromeCDP
import time

cdp = ChromeCDP()

USERNAME_XPATH = "/html/body/div[1]/div/div[2]/div[1]/div/div/form/div[1]/input"
PASSWORD_XPATH = "/html/body/div[1]/div/div[2]/div[1]/div/div/form/div[2]/input"
LOGIN_XPATH = "/html/body/div[1]/div/div[2]/div[1]/div/div/form/input"
HEADER_XPATH = "//*[text()='Swag Labs']"

print("Launching Chrome...")
cdp.launch()

print("Navigating to SauceDemo...")
cdp.navigate("https://www.saucedemo.com/")

print("Waiting for page header 'Swag Labs'...")
cdp.wait_for_text('Swag Labs', timeout_ms=10000)

#print("Waiting for page header 'Swag Labs'...")
#cdp.wait_for_text('Swag Labs', timeout_ms=10000)

print("Typing username...")
cdp.fill(USERNAME_XPATH, "standard_user")

print("Typing password...")
cdp.fill(PASSWORD_XPATH, "secret_sauce")

print("Clicking Login...")
cdp.click(LOGIN_XPATH)

print("DONE — Login attempted")
