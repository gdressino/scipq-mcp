package com.example.shop;

import javax.ws.rs.GET;
import javax.ws.rs.POST;
import javax.ws.rs.Path;

/** Endpoints: the route is the class's path, the method's own and its verb. */
@Path("/orders")
public class OrderResource {
    @POST
    public String create() {
        return Steps.first();
    }

    @GET
    @Path("/{id}")
    public String find() {
        return new OrderDao().openFilter();
    }
}
