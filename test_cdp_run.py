from cdp_client import ChromeCDP
import time

cdp = ChromeCDP()

USERNAME_XPATH = "/html/body/div[1]/div/div[2]/div[1]/div/div/form/div[1]/input"
PASSWORD_XPATH = "/html/body/div[1]/div/div[2]/div[1]/div/div/form/div[2]/input"
LOGIN_XPATH = "/html/body/div[1]/div/div[2]/div[1]/div/div/form/input"
HEADER_XPATH = "//*[text()='Swag Labs']"
BACKPACK_XPATH = "/html/body/div/div/div/div[2]/div/div/div/div[1]/div[2]/div[2]/button"
BIKE_LIGHT_XPATH= "/html/body/div/div/div/div[2]/div/div/div/div[2]/div[2]/div[2]/button"
RED_TSHIRT_XPATH = "/html/body/div/div/div/div[2]/div/div/div/div[6]/div[2]/div[2]/button"
VIEW_CART_XPATH = "/html/body/div/div/div/div[1]/div[1]/div[3]/a"
CHECKOUT_XPATH = "/html/body/div/div/div/div[2]/div/div[2]/button[2]"
FIRSTNAME_XPATH = "/html/body/div/div/div/div[2]/div/form/div[1]/div[1]/input"
LASTNAME_XPATH = "/html/body/div/div/div/div[2]/div/form/div[1]/div[2]/input"
POSTALCODE_XPATH = "/html/body/div/div/div/div[2]/div/form/div[1]/div[3]/input"
CONTINUE_XPATH = "/html/body/div/div/div/div[2]/div/form/div[2]/input"
FINISH_XPATH = "/html/body/div/div/div/div[2]/div/div[2]/div[9]/button[2]"

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

print("Add items to cart...")
print("Adding Backpack...")
cdp.click(BACKPACK_XPATH)

print("Adding Bike Light...")
cdp.click(BIKE_LIGHT_XPATH)
print("Adding Red T-Shirt...")
cdp.click(RED_TSHIRT_XPATH)

print("Viewing Cart...")
cdp.click(VIEW_CART_XPATH)

print("Proceeding to Checkout...")
cdp.click(CHECKOUT_XPATH)

print("Filling in Checkout details...")
cdp.fill(FIRSTNAME_XPATH, "John")
cdp.fill(LASTNAME_XPATH, "Doe")
cdp.fill(POSTALCODE_XPATH, "12345")
cdp.click(CONTINUE_XPATH)

print("Finishing Checkout...")
cdp.scroll_into_view(FINISH_XPATH)
cdp.click(FINISH_XPATH)