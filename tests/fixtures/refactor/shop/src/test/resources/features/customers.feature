Feature: customers

  Scenario: create one
    Given I create in the database a "Customer"
      | code | nickname |
      | C1   | first    |
    And I create in the database a "Product"
      | code | price |
      | P1   | 10    |
