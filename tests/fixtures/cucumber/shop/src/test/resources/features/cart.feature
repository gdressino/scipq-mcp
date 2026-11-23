@cart
Feature: Cart contents

  A cart counts what is added to it.

  Background:
    Given an empty cart named "main"

  Scenario: Add a few
    When I add 3 items
    Then the cart holds 3 items

  @slow
  Scenario Outline: Add <n>
    When I add <n> item
    Then the cart holds <n> item

    @pending
    Examples:
      | n |
      | 1 |
      | 2 |

  Scenario: Nobody defined this
    When I do something nobody wrote
