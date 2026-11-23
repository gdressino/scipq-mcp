package com.example.shop;

import io.cucumber.java.en.Given;
import io.cucumber.java.en.Then;
import io.cucumber.java.en.When;

/** Step definitions (glue) for the cart features. */
public class CartSteps {
    private Cart cart;

    @Given("an empty cart named {string}")
    public void anEmptyCart(String name) {
        cart = new Cart();
    }

    @When("I add {int} item(s)/once")
    public void iAdd(int count) {
        cart.add(count);
    }

    @Then("^the cart holds (\\d+) items?$")
    public void theCartHolds(int count) {
        if (cart.size() != count) {
            throw new AssertionError("size " + cart.size());
        }
    }

    @Given("the background from {string}")
    public void theBackgroundFrom(String path) {
        // Runs the Background steps of another feature (see [cucumber] include_steps).
    }

    @Given("a coupon {string}")
    public void aCoupon(String code) {
    }

    @Then("nobody " + "uses this step")
    public void unused() {
    }
}
