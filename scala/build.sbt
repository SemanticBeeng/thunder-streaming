name := "thunder-streaming"

version := "0.1.0_dev"

scalaVersion := "2.10.3"

ivyXML := <dependency org="org.eclipse.jetty.orbit" name="javax.servlet" rev="3.0.0.v201112011016">
<artifact name="javax.servlet" type="orbit" ext="jar"/>
</dependency>

net.virtualvoid.sbt.graph.Plugin.graphSettings

libraryDependencies += "org.apache.hadoop" % "hadoop-client" % "1.0.4"

libraryDependencies += "org.apache.spark" %% "spark-core" % "1.2.1" excludeAll(
  ExclusionRule(organization = "org.apache.hadoop")
  )

libraryDependencies += "org.apache.spark" %% "spark-streaming" % "1.2.1" excludeAll(
  ExclusionRule(organization = "org.apache.hadoop")
  )

libraryDependencies += "org.apache.spark" % "spark-mllib_2.10" % "1.2.1" excludeAll(
  ExclusionRule(organization = "org.apache.hadoop")
  )

libraryDependencies += "colt" % "colt" % "1.2.0"

libraryDependencies += "org.scalatest" % "scalatest_2.10" % "2.0" % "test"

libraryDependencies += "org.jblas" % "jblas" % "1.2.3"

resolvers += "spray" at "http://repo.spray.io/"

libraryDependencies += "io.spray" %%  "spray-json" % "1.3.1"

resolvers ++= Seq(
  "Akka Repository" at "http://repo.akka.io/releases/",
  "Spray Repository" at "http://repo.spray.cc/")

resolvers += "Sonatype (releases)" at "https://oss.sonatype.org/content/repositories/releases/"

libraryDependencies += "org.zeromq" % "jeromq" % "0.3.4"
