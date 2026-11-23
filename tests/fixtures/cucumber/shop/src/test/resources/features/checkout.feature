@checkout
Feature: Checkout

  Background:
    Given the background from "features/fixtures/store.feature"

  Scenario: Pay
    Given an empty cart named "pay"
    When I add 1 item
    Then the cart holds 1 item

  Scenario: Pay with a missing background
    Given the background from "features/fixtures/missing.feature"
    When I add 2 items

  @pending
  Scenario: Later
    When I do something for later
