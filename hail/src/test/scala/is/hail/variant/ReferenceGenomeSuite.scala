package is.hail.variant

import java.io.FileNotFoundException

import is.hail.annotations.Region
import is.hail.asm4s.FunctionBuilder
import is.hail.check.Prop._
import is.hail.check.Properties
import is.hail.expr.ir.EmitFunctionBuilder
import is.hail.expr.types.virtual.{TLocus, TStruct}
import is.hail.io.reference.FASTAReader
import is.hail.table.Table
import is.hail.utils.{HailException, Interval, SerializableHadoopConfiguration}
import is.hail.testUtils._
import is.hail.{HailSuite, TestUtils}
import org.apache.spark.SparkException
import org.apache.spark.sql.Row
import org.testng.annotations.Test
import org.apache.hadoop

class ReferenceGenomeSuite extends HailSuite {
  @Test def testGRCh37() {
    val grch37 = ReferenceGenome.GRCh37
    assert(ReferenceGenome.hasReference("GRCh37"))

    assert(grch37.inX("X") && grch37.inY("Y") && grch37.isMitochondrial("MT"))
    assert(grch37.contigLength("1") == 249250621)

    val parXLocus = Array(Locus("X", 2499520), Locus("X", 155260460))
    val parYLocus = Array(Locus("Y", 50001), Locus("Y", 59035050))
    val nonParXLocus = Array(Locus("X", 50), Locus("X", 50000000))
    val nonParYLocus = Array(Locus("Y", 5000), Locus("Y", 10000000))

    assert(parXLocus.forall(grch37.inXPar) && parYLocus.forall(grch37.inYPar))
    assert(!nonParXLocus.forall(grch37.inXPar) && !nonParYLocus.forall(grch37.inYPar))
  }

  @Test def testGRCh38() {
    val grch38 = ReferenceGenome.GRCh38
    assert(ReferenceGenome.hasReference("GRCh38"))

    assert(grch38.inX("chrX") && grch38.inY("chrY") && grch38.isMitochondrial("chrM"))
    assert(grch38.contigLength("chr1") == 248956422)

    val parXLocus38 = Array(Locus("chrX", 2781479), Locus("chrX", 156030895))
    val parYLocus38 = Array(Locus("chrY", 50001), Locus("chrY", 57217415))
    val nonParXLocus38 = Array(Locus("chrX", 50), Locus("chrX", 50000000))
    val nonParYLocus38 = Array(Locus("chrY", 5000), Locus("chrY", 10000000))

    assert(parXLocus38.forall(grch38.inXPar) && parYLocus38.forall(grch38.inYPar))
    assert(!nonParXLocus38.forall(grch38.inXPar) && !nonParYLocus38.forall(grch38.inYPar))
  }

  @Test def testAssertions() {
    TestUtils.interceptFatal("Must have at least one contig in the reference genome.")(ReferenceGenome("test", Array.empty[String], Map.empty[String, Int]))
    TestUtils.interceptFatal("No lengths given for the following contigs:")(ReferenceGenome("test", Array("1", "2", "3"), Map("1" -> 5)))
    TestUtils.interceptFatal("Contigs found in 'lengths' that are not present in 'contigs'")(ReferenceGenome("test", Array("1", "2", "3"), Map("1" -> 5, "2" -> 5, "3" -> 5, "4" -> 100)))
    TestUtils.interceptFatal("The following X contig names are absent from the reference:")(ReferenceGenome("test", Array("1", "2", "3"), Map("1" -> 5, "2" -> 5, "3" -> 5), xContigs = Set("X")))
    TestUtils.interceptFatal("The following Y contig names are absent from the reference:")(ReferenceGenome("test", Array("1", "2", "3"), Map("1" -> 5, "2" -> 5, "3" -> 5), yContigs = Set("Y")))
    TestUtils.interceptFatal("The following mitochondrial contig names are absent from the reference:")(ReferenceGenome("test", Array("1", "2", "3"), Map("1" -> 5, "2" -> 5, "3" -> 5), mtContigs = Set("MT")))
    TestUtils.interceptFatal("The contig name for PAR interval")(ReferenceGenome("test", Array("1", "2", "3"), Map("1" -> 5, "2" -> 5, "3" -> 5), parInput = Array((Locus("X", 1), Locus("X", 5)))))
    TestUtils.interceptFatal("in both X and Y contigs.")(ReferenceGenome("test", Array("1", "2", "3"), Map("1" -> 5, "2" -> 5, "3" -> 5), xContigs = Set("1"), yContigs = Set("1")))
  }

  @Test def testContigRemap() {
    val mapping = Map("23" -> "foo")
    TestUtils.interceptFatal("have remapped contigs in reference genome")(ReferenceGenome.GRCh37.validateContigRemap(mapping))
  }

  @Test def testComparisonOps() {
    val rg = ReferenceGenome.GRCh37

    // Test contigs
    assert(rg.compare("3", "18") < 0)
    assert(rg.compare("18", "3") > 0)
    assert(rg.compare("7", "7") == 0)

    assert(rg.compare("3", "X") < 0)
    assert(rg.compare("X", "3") > 0)
    assert(rg.compare("X", "X") == 0)

    assert(rg.compare("X", "Y") < 0)
    assert(rg.compare("Y", "X") > 0)
    assert(rg.compare("Y", "MT") < 0)

    assert(rg.compare("18", "SPQR") < 0)
    assert(rg.compare("MT", "SPQR") < 0)

    // Test loci
    val l1 = Locus("1", 25)
    val l2 = Locus("1", 13000)
    val l3 = Locus("2", 26)
  }

  @Test def testWriteToFile() {
    val tmpFile = tmpDir.createTempFile("grWrite", ".json")

    val rg = ReferenceGenome.GRCh37
    rg.copy(name = "GRCh37_2").write(hc.sFS, tmpFile)
    val gr2 = ReferenceGenome.fromFile(hc, tmpFile)

    assert((rg.contigs sameElements gr2.contigs) &&
      rg.lengths == gr2.lengths &&
      rg.xContigs == gr2.xContigs &&
      rg.yContigs == gr2.yContigs &&
      rg.mtContigs == gr2.mtContigs &&
      (rg.parInput sameElements gr2.parInput))
  }

  @Test def testFasta() {
    val fastaFile = "src/test/resources/fake_reference.fasta"
    val fastaFileGzip = "src/test/resources/fake_reference.fasta.gz"
    val indexFile = "src/test/resources/fake_reference.fasta.fai"

    val rg = ReferenceGenome("test", Array("a", "b", "c"), Map("a" -> 25, "b" -> 15, "c" -> 10))
    ReferenceGenome.addReference(rg)

    val fr = FASTAReader(hc, rg, fastaFile, indexFile, 3, 5)
    val frGzip = FASTAReader(hc, rg, fastaFileGzip, indexFile, 3, 5)

    object Spec extends Properties("Fasta Random") {
      property("cache gives same base as from file") = forAll(Locus.gen(rg)) { l =>
        val contig = l.contig
        val pos = l.position
        val expected = fr.reader.value.getSubsequenceAt(contig, pos, pos).getBaseString
        fr.lookup(contig, pos, 0, 0) == expected && frGzip.lookup(contig, pos, 0, 0) == expected
      }

      val ordering = TLocus(rg).ordering
      property("interval test") = forAll(Interval.gen(ordering, Locus.gen(rg))) { i =>
        val start = i.start.asInstanceOf[Locus]
        val end = i.end.asInstanceOf[Locus]

        def getHtsjdkIntervalSequence: String = {
          val sb = new StringBuilder
          var pos = start
          while (ordering.lteq(pos, end) && pos != null) {
            val endPos = if (pos.contig != end.contig) rg.contigLength(pos.contig) else end.position
            sb ++= fr.reader.value.getSubsequenceAt(pos.contig, pos.position, endPos).getBaseString
            pos =
              if (rg.contigsIndex(pos.contig) == rg.contigs.length - 1)
                null
              else
                Locus(rg.contigs(rg.contigsIndex(pos.contig) + 1), 1)
          }
          sb.result()
        }

        fr.lookup(Interval(start, end, includesStart = true, includesEnd = true)) == getHtsjdkIntervalSequence
      }
    }

    Spec.check()

    assert(fr.lookup("a", 25, 0, 5) == "A")
    assert(fr.lookup("b", 1, 5, 0) == "T")
    assert(fr.lookup("c", 5, 10, 10) == "GGATCCGTGC")
    assert(fr.lookup(Interval(Locus("a", 1), Locus("a", 5), includesStart = true, includesEnd = false)) == "AGGT")
    assert(fr.lookup(Interval(Locus("a", 20), Locus("b", 5), includesStart = false, includesEnd = false)) == "ACGTATAAT")
    assert(fr.lookup(Interval(Locus("a", 20), Locus("c", 5), includesStart = false, includesEnd = false)) == "ACGTATAATTAAATTAGCCAGGAT")
  }

  @Test def testSerializeOnFB() {
    val grch38 = ReferenceGenome.GRCh38
    val fb = EmitFunctionBuilder[String, Boolean]("serialize_rg")

    val rgfield = fb.newLazyField(grch38.codeSetup(fb))
    fb.emit(rgfield.invoke[String, Boolean]("isValidContig", fb.getArg[String](1)))

    Region.scoped { r =>
      val f = fb.resultWithIndex()(0, r)
      assert(f("X") == grch38.isValidContig("X"))
    }
  }

  @Test def testSerializeWithFastaOnFB() {
    val fastaFile = "src/test/resources/fake_reference.fasta"
    val indexFile = "src/test/resources/fake_reference.fasta.fai"

    val rg = ReferenceGenome("test", Array("a", "b", "c"), Map("a" -> 25, "b" -> 15, "c" -> 10))
    ReferenceGenome.addReference(rg)
    rg.addSequence(hc, fastaFile, indexFile)

    val fb = EmitFunctionBuilder[String, Int, Int, Int, String]("serialize_rg")

    val rgfield = fb.newLazyField(rg.codeSetup(fb))
    fb.emit(rgfield.invoke[String, Int, Int, Int, String]("getSequence", fb.getArg[String](1), fb.getArg[Int](2), fb.getArg[Int](3), fb.getArg[Int](4)))

    Region.scoped { r =>
      val f = fb.resultWithIndex()(0, r)
      assert(f("a", 25, 0, 5) == rg.getSequence("a", 25, 0, 5))
    }
  }

  @Test def testSerializeWithLiftoverOnFB() {
    val grch37 = ReferenceGenome.GRCh37
    val liftoverFile = "src/test/resources/grch37_to_grch38_chr20.over.chain.gz"

    grch37.addLiftover(hc, liftoverFile, "GRCh38")

    val fb = EmitFunctionBuilder[String, Locus, Double, (Locus, Boolean)]("serialize_with_liftover")
    val rgfield = fb.newLazyField(grch37.codeSetup(fb))
    fb.emit(rgfield.invoke[String, Locus, Double, (Locus, Boolean)]("liftoverLocus", fb.getArg[String](1), fb.getArg[Locus](2), fb.getArg[Double](3)))

    Region.scoped { r =>
      val f = fb.resultWithIndex()(0, r)
      assert(f("GRCh38", Locus("20", 60001), 0.95) == grch37.liftoverLocus("GRCh38", Locus("20", 60001), 0.95))
      grch37.removeLiftover("GRCh38")
    }
  }
}
